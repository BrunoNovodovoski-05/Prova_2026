import hashlib
import json
import sys


PREFIXOS = [
    "A",
    "B",
    "C",
    "D",
    "E",
    "F",
]

RAZOES = [
    2,
    3,
]

PORTA_BASE = 9201

FAIXA = 5


def variante(slug):
    numero = int(
        hashlib.sha256(
            slug.encode("utf-8")
        ).hexdigest(),
        16,
    )

    return {
        "repositorio": slug,
        "prefixo": PREFIXOS[
            numero % len(PREFIXOS)
        ],
        "razao_preferencial": RAZOES[
            numero % len(RAZOES)
        ],
        "porta_api": PORTA_BASE + (
            numero % FAIXA
        ),
    }


def main():

    if len(sys.argv) != 2:
        raise SystemExit(
            "Uso: python gerar_params.py NOME_EXATO_DO_REPOSITORIO"
        )

    slug = sys.argv[1]

    dados = variante(slug)

    with open(
        "params.json",
        "w",
        encoding="utf-8",
    ) as arquivo:

        json.dump(
            dados,
            arquivo,
            ensure_ascii=False,
            indent=2,
        )

    print(
        json.dumps(
            dados,
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
