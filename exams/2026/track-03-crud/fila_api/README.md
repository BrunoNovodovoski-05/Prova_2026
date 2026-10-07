# Fila de Atendimento

## 1. Gerar a variante

Execute uma vez com o nome EXATO do repositório:

```bash
python gerar_params.py nome-exato-do-repositorio
```

Isso materializa `params.json`, que é copiado para o container e não depende de variável de ambiente.

## 2. Rodar localmente

```bash
pip install -r requirements.txt
python app.py
```

API: `http://localhost:8080`

## 3. Build do container

```bash
docker build -f Containerfile -t fila-atendimento .
```

Confira a porta externa em `params.json` e rode, por exemplo:

```bash
docker run --rm -p 9201:8080 fila-atendimento
```

Para testar persistência escondida equivalente à correção:

```bash
docker volume create fila-data
docker run --rm -p 9201:8080 -v fila-data:/data fila-atendimento
```

## Endpoints

- `GET /healthz`
- `POST /senhas`
- `GET /senhas/proxima`
- `POST /senhas/{codigo}/concluir`
- `POST /senhas/{codigo}/rechamar`
- `POST /senhas/{codigo}/cancelar`
- `GET /painel`
