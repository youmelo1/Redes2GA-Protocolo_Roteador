# Protocolo de Roteamento Dinâmico (Vetor de Distância com Métricas Compostas)

Este projeto, desenvolvido para a disciplina de Fundamentos de Sistemas Operacionais, implementa um protocolo de roteamento dinâmâmico em Python. Ele foi projetado para ser executado e testado num ambiente de rede virtualizado com Docker e Docker Compose, demonstrando conceitos avançados de roteamento de rede.

## Funcionalidades Principais

-   **Protocolo Vetor de Distância:** Baseado na lógica do algoritmo Bellman-Ford, similar ao RIP. Os roteadores trocam as suas tabelas de roteamento com os vizinhos para descobrir a topologia da rede.
-   **Métricas Compostas e Dinâmicas:** A decisão de qual caminho é o melhor não se baseia apenas em saltos, mas sim num **custo composto** a partir de múltiplas métricas, onde a penalidade de congestão é calculada dinamicamente com base no número de vizinhos ativos.
-   **Adaptação Dinâmica a Falhas e Recuperações:** O protocolo implementa múltiplos mecanismos para garantir a estabilidade da rede e a rápida convergência após falhas ou recuperações de links.
-   **Sincronização com o Sistema Operacional:** O protocolo aplica as rotas calculadas diretamente na tabela de roteamento do kernel Linux dentro do container, tornando-o um agente de rede funcional.

## Como Funciona

### Cálculo de Métricas (Custo do Link)
A "inteligência" do protocolo reside no método `_calculate_composite_cost`. Ele combina três fatores para determinar o "custo" de um link para um vizinho:
1.  **Latência (`latency_ms`):** Contribui diretamente para o custo. Menor latência = melhor.
2.  **Largura de Banda (`bandwidth_mbps`):** Contribui de forma inversa (`1000 / largura`). Maior largura de banda = melhor.
3.  **Congestão (Dinâmica):** Uma pequena penalidade é adicionada com base no número de vizinhos que estão **atualmente ativos** (online). Se um vizinho cai, o roteador torna-se "menos congestionado" e o custo dos seus links de saída diminui. Se um vizinho volta a ficar online, o custo aumenta. Isso faz com que o protocolo possa desviar o tráfego de forma inteligente, reagindo a mudanças na topologia da vizinhança.

### Mecanismos de Robustez
Para evitar loops de roteamento e garantir a rápida convergência, o protocolo implementa quatro técnicas padrão da indústria:

1.  **Timeout de Vizinhos:** Se um roteador não recebe notícias de um vizinho direto por 30 segundos (`TIMEOUT_INTERVAL`), ele considera que o vizinho caiu.
2.  **Route Poisoning (Envenenamento de Rota):** Ao detetar um timeout, o roteador não apaga simplesmente as rotas que dependiam daquele vizinho. Em vez disso, ele as mantém na sua tabela mas define o seu custo como "infinito" (`INFINITY = 999`). Esta rota "envenenada" é então anunciada aos outros vizinhos, funcionando como uma notificação explícita e rápida de que o caminho morreu.
3.  **Split Horizon with Poison Reverse:** Para evitar que a informação de uma rota seja refletida de volta e crie um loop (problema conhecido como *counting to infinity*), o protocolo segue a regra: "Eu nunca vou anunciar uma rota de volta para o vizinho de quem eu a aprendi com um custo válido". Em vez disso, ele anuncia essa rota com custo infinito, reforçando que o caminho passa por aquele vizinho.
4.  **Hold-Down Timers:** Após uma rota ser marcada como inalcançável (envenenada), o roteador ativa um "temporizador de espera" de 60 segundos (`HOLD_DOWN_INTERVAL`) para aquele destino. Durante este período, ele ignora quaisquer outras atualizações sobre aquele destino. Isso dá tempo para que a "má notícia" se propague por toda a rede de forma consistente, evitando que seja contradita por informações antigas que ainda estejam em trânsito.

## Estrutura dos Arquivos

-   `simple_router.py`: O código fonte principal do roteador, contendo toda a lógica do protocolo.
-   `Dockerfile`: Define a imagem Docker para a aplicação, incluindo as dependências de rede (`iproute2`).
-   `docker-compose.yml`: Orquestra a criação da rede virtual e de todos os roteadores.
-   `configs/`: Pasta que contém os arquivos `config_rX.json` para cada roteador.

## Como Executar e Testar

**Pré-requisitos:**
-   Docker
-   Docker Compose

**1. Iniciar a Rede Completa:**
Para construir as imagens e iniciar todos os roteadores em modo interativo.
```bash
docker-compose up --build
```
(Para rodar em segundo plano, adicione a flag `-d`).

**2. Verificar o Status dos Roteadores:**
```bash
docker-compose ps
```

**3. Visualizar os Logs de um Roteador:**
Para ver as tabelas de roteamento e as mensagens de um roteador (ex: `r1`) em tempo real.
```bash
docker-compose logs -f r1
```

**4. Inspecionar a Tabela de Roteamento do S.O.:**
Para provar que a sincronização funcionou, execute o `ip route` dentro de um container.
```bash
docker exec r1 ip route
```

**5. Simular uma Falha (Parar um Roteador):**
```bash
docker-compose stop r2
```

**6. Recuperar de uma Falha (Iniciar um Roteador):**
```bash
docker-compose start r2
```

**7. Parar e Limpar Todo o Ambiente:**
```bash
docker-compose down -v
```

## Como Adicionar um Novo Roteador (ex: `r5`)

Para expandir a topologia da rede, siga os passos abaixo.

**1. Edite `docker-compose.yml`:**
Copie o bloco de serviço de um roteador existente e altere os valores para `r5`.
```yaml
# Adicione este bloco ao final da seção 'services' em docker-compose.yml
r5:
  build: .
  container_name: r5
  networks:
    roteamento-net:
      ipv4_address: X.X.X.X   # Escolha um novo IP Fixo (ex: 172.28.0.105)
  cap_add:
    - NET_ADMIN
  command: python simple_router.py --config configs/config_r5.json
```

**2. Atualize o Mapa da Rede:**
Adicione a entrada para o novo roteador `r5` ao `network_map` em **TODOS** os arquivos de configuração existentes (`config_r1.json`, `config_r2.json`, etc.).
```json
"network_map": {
    "r1": "10.0.1.0/24",
    "r2": "10.0.2.0/24",
    "r3": "10.0.3.0/24",
    "r4": "10.0.4.0/24",
    "r5": "X.X.X.X/XX"  # Adicione a nova rede (ex: "10.0.5.0/24")
},
```

**3. Crie `configs/config_r5.json`:**
Crie o novo arquivo de configuração para `r5`, definindo a sua identidade e os seus vizinhos.

**Exemplo de `configs/config_r5.json` (conectando-se apenas ao `r4`):**
```json
{
    "router_id": "r5",
    "listen_port": XXXX,
    "network_map": {
        "r1": "10.0.1.0/24",
        "r2": "10.0.2.0/24",
        "r3": "10.0.3.0/24",
        "r4": "10.0.4.0/24",
        "r5": "10.0.5.0/24"
    },
    "neighbors": [
        {
            "id": "r4",
            "ip": "IP_DO_R4",
            "port": YYYY,
            "metrics": {
                "bandwidth_mbps": ZZZ,
                "latency_ms": WW
            }
        }
    ]
}
```

**4. Defina a Conexão no Vizinho:**
Não se esqueça de atualizar a configuração do roteador ao qual o `r5` irá se conectar.
* **Em `configs/config_r4.json`:** Adicione o `r5` à lista de `neighbors`, garantindo que as métricas sejam simétricas às definidas no passo anterior.

**5. Reinicie a rede:**
```bash
docker-compose up -d --build
```
