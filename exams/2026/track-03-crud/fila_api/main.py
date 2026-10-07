import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, request


TZ = timezone(timedelta(hours=-3))
PREFIXOS = ["A", "B", "C", "D", "E", "F"]
RAZOES = [2, 3]
PORTA_BASE = 9201
FAIXA = 5

BASE_DIR = Path(__file__).resolve().parent


def carregar_variante():
    """
    Carrega params.json se existir.
    Caso contrário, tenta derivar pelo nome do diretório do projeto.

    Formato aceito de params.json:
    {
      "repositorio": "nome-exato-do-repositorio"
    }

    Também aceita os campos já materializados:
    {
      "prefixo": "A",
      "razao_preferencial": 2,
      "porta_api": 9201
    }
    """
    params_path = BASE_DIR / "params.json"

    if params_path.exists():
        with params_path.open("r", encoding="utf-8") as f:
            dados = json.load(f)

        if {
            "prefixo",
            "razao_preferencial",
            "porta_api",
        }.issubset(dados):
            return (
                str(dados["prefixo"]),
                int(dados["razao_preferencial"]),
                int(dados["porta_api"]),
            )

        nome_repo = dados.get("repositorio")
        if nome_repo:
            return derivar_variante(nome_repo)

    # Fallback útil em execução local. Dentro do container o diretório pode ser /app,
    # então para a correção real recomenda-se versionar params.json.
    return derivar_variante(BASE_DIR.name)


def derivar_variante(nome_repo: str):
    digest = hashlib.sha256(nome_repo.encode("utf-8")).digest()
    numero = int.from_bytes(digest, "big")

    prefixo = PREFIXOS[numero % len(PREFIXOS)]
    razao = RAZOES[numero % len(RAZOES)]
    porta = PORTA_BASE + (numero % FAIXA)

    return prefixo, razao, porta


PREFIXO, RAZAO_PREFERENCIAL, PORTA_API = carregar_variante()


def escolher_banco():
    data_dir = Path("/data")

    try:
        data_dir.mkdir(parents=True, exist_ok=True)

        teste = data_dir / ".write_test"
        teste.write_text("ok", encoding="utf-8")
        teste.unlink(missing_ok=True)

        return data_dir / "fila.db"

    except Exception:
        return BASE_DIR / "fila.db"


DB_PATH = escolher_banco()

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


@contextmanager
def conexao(immediate=False):
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        isolation_level=None,
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")

    if immediate:
        conn.execute("BEGIN IMMEDIATE")

    try:
        yield conn

        if immediate:
            conn.execute("COMMIT")

    except Exception:
        if immediate:
            conn.execute("ROLLBACK")

        raise

    finally:
        conn.close()


def agora_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def hoje_local():
    return datetime.now(TZ).date().isoformat()


def init_db():
    with conexao() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS senhas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo TEXT NOT NULL,
                data_emissao TEXT NOT NULL,
                tipo TEXT NOT NULL CHECK(tipo IN ('normal', 'preferencial')),
                emissao TEXT NOT NULL,
                status TEXT NOT NULL CHECK(
                    status IN (
                        'aguardando',
                        'chamada',
                        'concluida',
                        'cancelada'
                    )
                ),
                chamada_em TEXT,
                UNIQUE(data_emissao, codigo)
            );

            CREATE TABLE IF NOT EXISTS estado (
                chave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS painel_eventos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                senha_id INTEGER NOT NULL,
                chamada_em TEXT NOT NULL,
                FOREIGN KEY(senha_id) REFERENCES senhas(id)
            );
            """
        )

        conn.execute(
            """
            INSERT OR IGNORE
            INTO estado(chave, valor)
            VALUES('data_sequencia', ?)
            """,
            (hoje_local(),),
        )

        conn.execute(
            """
            INSERT OR IGNORE
            INTO estado(chave, valor)
            VALUES('sequencia', '0')
            """
        )

        conn.execute(
            """
            INSERT OR IGNORE
            INTO estado(chave, valor)
            VALUES('preferenciais_no_ciclo', '0')
            """
        )


def get_estado(conn, chave, padrao=None):
    row = conn.execute(
        """
        SELECT valor
        FROM estado
        WHERE chave = ?
        """,
        (chave,),
    ).fetchone()

    return row["valor"] if row else padrao


def set_estado(conn, chave, valor):
    conn.execute(
        """
        INSERT INTO estado(chave, valor)
        VALUES(?, ?)

        ON CONFLICT(chave)
        DO UPDATE SET valor = excluded.valor
        """,
        (
            chave,
            str(valor),
        ),
    )


def serializar_senha(row):
    dados = {
        "codigo": row["codigo"],
        "tipo": row["tipo"],
        "emissao": row["emissao"],
        "status": row["status"],
    }

    if row["chamada_em"] is not None:
        dados["chamada_em"] = row["chamada_em"]

    return dados


def buscar_senha(conn, codigo):
    # Como a sequência reinicia diariamente, o mesmo código pode reaparecer
    # em dias diferentes. O endpoint por código opera sobre a ocorrência mais recente.

    return conn.execute(
        """
        SELECT
            id,
            codigo,
            tipo,
            emissao,
            status,
            chamada_em

        FROM senhas

        WHERE codigo = ?

        ORDER BY
            data_emissao DESC,
            id DESC

        LIMIT 1
        """,
        (codigo,),
    ).fetchone()


def buscar_senha_por_id(conn, senha_id):
    return conn.execute(
        """
        SELECT
            id,
            codigo,
            tipo,
            emissao,
            status,
            chamada_em

        FROM senhas

        WHERE id = ?
        """,
        (senha_id,),
    ).fetchone()


def primeira_aguardando(conn, tipo):
    return conn.execute(
        """
        SELECT
            id,
            codigo,
            tipo,
            emissao,
            status,
            chamada_em

        FROM senhas

        WHERE
            status = 'aguardando'
            AND tipo = ?

        ORDER BY
            emissao ASC,
            codigo ASC

        LIMIT 1
        """,
        (tipo,),
    ).fetchone()


@app.get("/healthz")
def healthz():
    return jsonify(
        {
            "status": "ok"
        }
    ), 200


@app.post("/senhas")
def emitir_senha():
    dados = request.get_json(silent=True) or {}

    tipo = dados.get("tipo")

    if tipo not in ("normal", "preferencial"):
        return jsonify(
            {
                "erro": "tipo_invalido"
            }
        ), 422

    with conexao(immediate=True) as conn:
        data_atual = hoje_local()

        data_seq = get_estado(
            conn,
            "data_sequencia",
            data_atual,
        )

        sequencia = int(
            get_estado(
                conn,
                "sequencia",
                "0",
            )
        )

        if data_seq != data_atual:
            sequencia = 0

            set_estado(
                conn,
                "data_sequencia",
                data_atual,
            )

            set_estado(
                conn,
                "sequencia",
                0,
            )

        sequencia += 1

        set_estado(
            conn,
            "sequencia",
            sequencia,
        )

        codigo = f"{PREFIXO}{sequencia:03d}"

        emissao = agora_iso()

        conn.execute(
            """
            INSERT INTO senhas(
                codigo,
                data_emissao,
                tipo,
                emissao,
                status,
                chamada_em
            )

            VALUES(
                ?,
                ?,
                ?,
                ?,
                'aguardando',
                NULL
            )
            """,
            (
                codigo,
                data_atual,
                tipo,
                emissao,
            ),
        )

        row = buscar_senha(
            conn,
            codigo,
        )

        return jsonify(
            serializar_senha(row)
        ), 201


@app.get("/senhas/proxima")
def proxima_senha():
    with conexao(immediate=True) as conn:
        pref = primeira_aguardando(
            conn,
            "preferencial",
        )

        normal = primeira_aguardando(
            conn,
            "normal",
        )

        if pref is None and normal is None:
            return jsonify(
                {
                    "erro": "fila_vazia"
                }
            ), 404

        usados = int(
            get_estado(
                conn,
                "preferenciais_no_ciclo",
                "0",
            )
        )

        if pref is not None and normal is not None:
            if usados < RAZAO_PREFERENCIAL:
                escolhida = pref
                usados += 1

            else:
                escolhida = normal
                usados = 0

        elif pref is not None:
            escolhida = pref

            usados = min(
                usados + 1,
                RAZAO_PREFERENCIAL,
            )

        else:
            escolhida = normal

            usados = 0

        set_estado(
            conn,
            "preferenciais_no_ciclo",
            usados,
        )

        chamada_em = agora_iso()

        conn.execute(
            """
            UPDATE senhas

            SET
                status = 'chamada',
                chamada_em = ?

            WHERE codigo = ?
            """,
            (
                chamada_em,
                escolhida["codigo"],
            ),
        )

        conn.execute(
            """
            INSERT INTO painel_eventos(
                senha_id,
                chamada_em
            )

            VALUES(?, ?)
            """,
            (
                escolhida["id"],
                chamada_em,
            ),
        )

        row = buscar_senha(
            conn,
            escolhida["codigo"],
        )

        return jsonify(
            serializar_senha(row)
        ), 200


@app.post("/senhas/<codigo>/concluir")
def concluir_senha(codigo):
    with conexao(immediate=True) as conn:
        row = buscar_senha(
            conn,
            codigo,
        )

        if row is None:
            return jsonify(
                {
                    "erro": "senha_nao_encontrada"
                }
            ), 404

        if row["status"] != "chamada":
            return jsonify(
                {
                    "erro": "senha_nao_chamada"
                }
            ), 409

        conn.execute(
            """
            UPDATE senhas
            SET status = 'concluida'
            WHERE codigo = ?
            """,
            (codigo,),
        )

        row = buscar_senha(
            conn,
            codigo,
        )

        return jsonify(
            serializar_senha(row)
        ), 200


@app.post("/senhas/<codigo>/rechamar")
def rechamar_senha(codigo):
    with conexao(immediate=True) as conn:
        row = buscar_senha(
            conn,
            codigo,
        )

        if row is None:
            return jsonify(
                {
                    "erro": "senha_nao_encontrada"
                }
            ), 404

        if row["status"] != "chamada":
            return jsonify(
                {
                    "erro": "senha_nao_chamada"
                }
            ), 409

        chamada_em = agora_iso()

        conn.execute(
            """
            UPDATE senhas

            SET chamada_em = ?

            WHERE codigo = ?
            """,
            (
                chamada_em,
                codigo,
            ),
        )

        conn.execute(
            """
            INSERT INTO painel_eventos(
                senha_id,
                chamada_em
            )

            VALUES(?, ?)
            """,
            (
                row["id"],
                chamada_em,
            ),
        )

        row = buscar_senha(
            conn,
            codigo,
        )

        return jsonify(
            serializar_senha(row)
        ), 200


@app.post("/senhas/<codigo>/cancelar")
def cancelar_senha(codigo):
    with conexao(immediate=True) as conn:
        row = buscar_senha(
            conn,
            codigo,
        )

        if row is None:
            return jsonify(
                {
                    "erro": "senha_nao_encontrada"
                }
            ), 404

        if row["status"] != "aguardando":
            return jsonify(
                {
                    "erro": "senha_nao_aguardando"
                }
            ), 409

        conn.execute(
            """
            UPDATE senhas

            SET status = 'cancelada'

            WHERE codigo = ?
            """,
            (codigo,),
        )

        row = buscar_senha(
            conn,
            codigo,
        )

        return jsonify(
            serializar_senha(row)
        ), 200


@app.get("/painel")
def painel():
    with conexao() as conn:
        eventos = conn.execute(
            """
            SELECT
                pe.senha_id,
                MAX(pe.id) AS ultimo_evento

            FROM painel_eventos pe

            GROUP BY pe.senha_id

            ORDER BY ultimo_evento DESC

            LIMIT 5
            """
        ).fetchall()

        chamadas = []

        for evento in eventos:
            row = buscar_senha_por_id(
                conn,
                evento["senha_id"],
            )

            if row is not None:
                chamadas.append(
                    serializar_senha(row)
                )

        return jsonify(
            {
                "chamadas": chamadas
            }
        ), 200


init_db()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8080,
        threaded=True,
    )
