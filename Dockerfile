# Dockerfile (Corrigido)
# Versão: Final-Definitiva-aaaaaaa

# Usar uma imagem base oficial do Python
FROM python:3.11-slim

# <<< ADICIONE ESTAS DUAS LINHAS >>>
# Atualiza a lista de pacotes e instala o 'iproute2' (que contém o comando 'ip')
RUN apt-get update && apt-get install -y iproute2

# Definir o diretório de trabalho dentro do container
WORKDIR /app

# Copiar todos os arquivos da pasta atual para dentro do container
COPY . .

# O comando padrão que será executado quando o container iniciar
CMD ["python", "simple_router.py"]