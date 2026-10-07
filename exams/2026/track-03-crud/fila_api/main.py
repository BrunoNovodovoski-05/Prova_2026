import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, request


# ============================================================
# CONFIGURAÇÃO
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
PARAMS_FILE = BASE_DIR / "params.json"

FUSO = timezone(timedelta(hours=-3))


def carregar_parametros():
    """
    Carrega a variante usada pela aplicação.

    Prioridade:
    1. Variáveis de ambiente, se fornecidas.
    2. params.json.

    Nenhuma variável de ambiente é obrigatória.
    """

    prefixo_env = os.environ.get("PREFIXO")
    razao_env = os.environ.get("RAZAO_PREFERENCIAL")

    if prefixo_env and razao_env:
        return prefixo_env, int(razao_env)

    if not PARAMS_FILE.exists():
        raise RuntimeError(
            "params.json não encontrado. "
            "Execute: python gerar_params.py NOME_DO_REPOSITORIO"
        )

    with open(PARAMS_FILE, "r", encoding="utf-8") as arquivo:
        dados = json.load(arquivo)

    if "prefixo" not in dados or "razao_preferencial" not in dados:
        raise RuntimeError(
            "params.json inválido. "
            "Execute gerar_params.py novamente."
        )

    return (
        str(dados["prefixo"]),
        int(dados["razao_preferencial"]),
    )


PREFIXO, RAZAO_PREFERENCIAL = carregar_parametros()


# ============================================================
# BANCO
# ============================================================

def caminho_banco():
    """
    Na correção escondida, /data será um volume persistente.

    Nos testes locais, se /data não puder ser usado,
    o banco fica no diretório da aplicação.
    """

    pasta_data = Path("/data")

    try:
        pasta_data.mkdir(parents=True, exist_ok=True)

        teste = pasta_data / ".teste_escrita"
        teste.write_text("ok", encoding="utf-8")
        teste.unlink()

        return pasta_data / "fila.db"

    except Exception:
        return BASE_DIR / "fila.db"


DB_PATH = caminho_banco()


@contextmanager
def conectar(escrita=False):
    conn = sqlite3.connect(
        str(DB_PATH),
        timeout=30,
        isolation_level=None,
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")

    if escrita:
        # Garante que duas emissões simultâneas não usem
        # a mesma sequência.
        conn.execute("BEGIN IMMEDIATE")

    try:
        yield conn

        if escrita:
            conn.execute("COMMIT")

    except Exception:
        if escrita:
            conn.execute("ROLLBACK")

        raise

    finally:
        conn.close()


def agora():
    return datetime.now(FUSO)


def agora_iso():
    return agora().isoformat(timespec="seconds")


def hoje():
    return agora().date().isoformat()


def iniciar_banco():
    with conectar(escrita=True) as conn:

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS senhas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                codigo TEXT NOT NULL,
                data_emissao TEXT NOT NULL,

                tipo TEXT NOT NULL
                    CHECK (
                        tipo IN (
                            'normal',
                            'preferencial'
                        )
                    ),

                emissao TEXT NOT NULL,

                status TEXT NOT NULL
                    CHECK (
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


            CREATE TABLE IF NOT EXISTS painel (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                senha_id INTEGER NOT NULL,

                chamada_em TEXT NOT NULL,

                FOREIGN KEY(senha_id)
                    REFERENCES senhas(id)
            );
            """
        )

        conn.execute(
            """
            INSERT OR IGNORE INTO estado(chave, valor)
            VALUES('data_sequencia', ?)
            """,
            (hoje(),),
        )

        conn.execute(
            """
            INSERT OR IGNORE INTO estado(chave, valor)
            VALUES('sequencia', '0')
            """
        )

        conn.execute(
            """
            INSERT OR IGNORE INTO estado(chave, valor)
            VALUES('preferenciais_no_ciclo', '0')
            """
        )


def obter_estado(conn, chave, padrao):
    row = conn.execute(
        """
        SELECT valor
        FROM estado
        WHERE chave = ?
        """,
        (chave,),
    ).fetchone()

    if row is None:
        return padrao

    return row["valor"]


def definir_estado(conn, chave, valor):
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


# ============================================================
# SENHAS
# ============================================================

def serializar(row):
    dados = {
        "codigo": row["codigo"],
        "tipo": row["tipo"],
        "emissao": row["emissao"],
        "status": row["status"],
    }

    if row["chamada_em"] is not None:
        dados["chamada_em"] = row["chamada_em"]

    return dados


def buscar_por_id(conn, senha_id):
    return conn.execute(
        """
        SELECT
            id,
            codigo,
            data_emissao,
            tipo,
            emissao,
            status,
            chamada_em

        FROM senhas

        WHERE id = ?
        """,
        (senha_id,),
    ).fetchone()


def buscar_por_codigo(conn, codigo):
    """
    A sequência reinicia diariamente.

    Portanto A001 pode existir em dias diferentes.

    Como a URL possui somente o código, utiliza-se
    a ocorrência mais recente.
    """

    return conn.execute(
        """
        SELECT
            id,
            codigo,
            data_emissao,
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


def primeira_aguardando(conn, tipo):
    """
    FIFO dentro de cada categoria.
    """

    return conn.execute(
        """
        SELECT
            id,
            codigo,
            data_emissao,
            tipo,
            emissao,
            status,
            chamada_em

        FROM senhas

        WHERE
            status = 'aguardando'
            AND tipo = ?

        ORDER BY id ASC

        LIMIT 1
        """,
        (tipo,),
    ).fetchone()


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

app.json.sort_keys = False


# ============================================================
# HEALTH
# ============================================================

@app.get("/healthz")
def healthz():
    return jsonify(
        {
            "status": "ok"
        }
    ), 200


# ============================================================
# EMITIR SENHA
# ============================================================

@app.post("/senhas")
def emitir_senha():
    dados = request.get_json(silent=True)

    if not isinstance(dados, dict):
        dados = {}

    tipo = dados.get("tipo")

    if tipo not in (
        "normal",
        "preferencial",
    ):
        return jsonify(
            {
                "erro": "tipo_invalido"
            }
        ), 422

    with conectar(escrita=True) as conn:

        data_atual = hoje()

        data_sequencia = obter_estado(
            conn,
            "data_sequencia",
            data_atual,
        )

        sequencia = int(
            obter_estado(
                conn,
                "sequencia",
                "0",
            )
        )

        # ================================================
        # REINÍCIO DIÁRIO
        # ================================================

        if data_sequencia != data_atual:

            sequencia = 0

            definir_estado(
                conn,
                "data_sequencia",
                data_atual,
            )

            definir_estado(
                conn,
                "sequencia",
                0,
            )

        sequencia += 1

        definir_estado(
            conn,
            "sequencia",
            sequencia,
        )

        codigo = f"{PREFIXO}{sequencia:03d}"

        emissao = agora_iso()

        cursor = conn.execute(
            """
            INSERT INTO senhas (
                codigo,
                data_emissao,
                tipo,
                emissao,
                status,
                chamada_em
            )

            VALUES (
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

        senha = buscar_por_id(
            conn,
            cursor.lastrowid,
        )

        return jsonify(
            serializar(senha)
        ), 201


# ============================================================
# PRÓXIMA SENHA
# ============================================================

@app.get("/senhas/proxima")
def proxima_senha():

    with conectar(escrita=True) as conn:

        preferencial = primeira_aguardando(
            conn,
            "preferencial",
        )

        normal = primeira_aguardando(
            conn,
            "normal",
        )

        if preferencial is None and normal is None:
            return jsonify(
                {
                    "erro": "fila_vazia"
                }
            ), 404

        usadas = int(
            obter_estado(
                conn,
                "preferenciais_no_ciclo",
                "0",
            )
        )

        # ================================================
        # HÁ NORMAL E PREFERENCIAL
        # ================================================

        if preferencial is not None and normal is not None:

            if usadas < RAZAO_PREFERENCIAL:

                escolhida = preferencial

                usadas += 1

            else:

                escolhida = normal

                usadas = 0

        # ================================================
        # SOMENTE PREFERENCIAL
        # ================================================

        elif preferencial is not None:

            escolhida = preferencial

            # Se já alcançou a razão, permanece saturada.
            #
            # Assim, se aparecer uma normal depois,
            # ela será chamada imediatamente.
            usadas = min(
                usadas + 1,
                RAZAO_PREFERENCIAL,
            )

        # ================================================
        # SOMENTE NORMAL
        # ================================================

        else:

            escolhida = normal

            usadas = 0

        definir_estado(
            conn,
            "preferenciais_no_ciclo",
            usadas,
        )

        chamada_em = agora_iso()

        # IMPORTANTE:
        # usamos o ID e não o código.
        conn.execute(
            """
            UPDATE senhas

            SET
                status = 'chamada',
                chamada_em = ?

            WHERE id = ?
            """,
            (
                chamada_em,
                escolhida["id"],
            ),
        )

        conn.execute(
            """
            INSERT INTO painel (
                senha_id,
                chamada_em
            )

            VALUES (?, ?)
            """,
            (
                escolhida["id"],
                chamada_em,
            ),
        )

        senha = buscar_por_id(
            conn,
            escolhida["id"],
        )

        return jsonify(
            serializar(senha)
        ), 200


# ============================================================
# CONCLUIR
# ============================================================

@app.post("/senhas/<codigo>/concluir")
def concluir(codigo):

    with conectar(escrita=True) as conn:

        senha = buscar_por_codigo(
            conn,
            codigo,
        )

        if senha is None:
            return jsonify(
                {
                    "erro": "senha_nao_encontrada"
                }
            ), 404

        if senha["status"] != "chamada":
            return jsonify(
                {
                    "erro": "senha_nao_chamada"
                }
            ), 409

        conn.execute(
            """
            UPDATE senhas

            SET status = 'concluida'

            WHERE id = ?
            """,
            (senha["id"],),
        )

        senha = buscar_por_id(
            conn,
            senha["id"],
        )

        return jsonify(
            serializar(senha)
        ), 200


# ============================================================
# RECHAMAR
# ============================================================

@app.post("/senhas/<codigo>/rechamar")
def rechamar(codigo):

    with conectar(escrita=True) as conn:

        senha = buscar_por_codigo(
            conn,
            codigo,
        )

        if senha is None:
            return jsonify(
                {
                    "erro": "senha_nao_encontrada"
                }
            ), 404

        if senha["status"] != "chamada":
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

            WHERE id = ?
            """,
            (
                chamada_em,
                senha["id"],
            ),
        )

        # Rechamada gera um novo evento no painel.
        conn.execute(
            """
            INSERT INTO painel (
                senha_id,
                chamada_em
            )

            VALUES (?, ?)
            """,
            (
                senha["id"],
                chamada_em,
            ),
        )

        senha = buscar_por_id(
            conn,
            senha["id"],
        )

        return jsonify(
            serializar(senha)
        ), 200


# ============================================================
# CANCELAR
# ============================================================

@app.post("/senhas/<codigo>/cancelar")
def cancelar(codigo):

    with conectar(escrita=True) as conn:

        senha = buscar_por_codigo(
            conn,
            codigo,
        )

        if senha is None:
            return jsonify(
                {
                    "erro": "senha_nao_encontrada"
                }
            ), 404

        if senha["status"] != "aguardando":
            return jsonify(
                {
                    "erro": "senha_nao_aguardando"
                }
            ), 409

        conn.execute(
            """
            UPDATE senhas

            SET status = 'cancelada'

            WHERE id = ?
            """,
            (senha["id"],),
        )

        senha = buscar_por_id(
            conn,
            senha["id"],
        )

        return jsonify(
            serializar(senha)
        ), 200


# ============================================================
# PAINEL
# ============================================================

@app.get("/painel")
def obter_painel():

    with conectar() as conn:

        eventos = conn.execute(
            """
            SELECT
                senha_id,
                MAX(id) AS ultimo_evento

            FROM painel

            GROUP BY senha_id

            ORDER BY ultimo_evento DESC

            LIMIT 5
            """
        ).fetchall()

        chamadas = []

        for evento in eventos:

            senha = buscar_por_id(
                conn,
                evento["senha_id"],
            )

            if senha is not None:
                chamadas.append(
                    serializar(senha)
                )

        return jsonify(
            {
                "chamadas": chamadas
            }
        ), 200


# ============================================================
# INICIALIZAÇÃO
# ============================================================

iniciar_banco()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8080,
        threaded=True,
    )
