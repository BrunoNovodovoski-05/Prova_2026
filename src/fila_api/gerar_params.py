import hashlib
import json
import sys

PREFIXOS = ["A", "B", "C", "D", "E", "F"]
RAZOES = [2, 3]
PORTA_BASE = 9201
FAIXA = 5

if len(sys.argv) != 2:
    raise SystemExit("Uso: python gerar_params.py NOME_EXATO_DO_REPOSITORIO")

nome = sys.argv[1]
numero = int.from_bytes(hashlib.sha256(nome.encode("utf-8")).digest(), "big")

dados = {
    "repositorio": nome,
    "prefixo": PREFIXOS[numero % len(PREFIXOS)],
    "razao_preferencial": RAZOES[numero % len(RAZOES)],
    "porta_api": PORTA_BASE + (numero % FAIXA),
}

with open("params.json", "w", encoding="utf-8") as f:
    json.dump(dados, f, ensure_ascii=False, indent=2)

print(json.dumps(dados, ensure_ascii=False, indent=2))
