# -*- coding: utf-8 -*-
"""
Implementação de um Protocolo de Roteamento Dinâmico (Vetor de Distância).

Este script define a lógica para um roteador que participa numa rede,
descobre a topologia e calcula as melhores rotas usando um algoritmo
de Vetor de Distância, similar ao RIP.

Funcionalidades implementadas:
- Métricas de Custo Compostas (latência, largura de banda, congestão).
- Sincronização com a tabela de roteamento do Kernel Linux.
- Mecanismos de robustez para evitar loops de roteamento:
  - Timeouts de Vizinhos.
  - Route Poisoning (Envenenamento de Rota).
  - Split Horizon with Poison Reverse.
  - Hold-Down Timers.
"""

import socket
import json
import time
import argparse
from pathlib import Path
import subprocess
import logging

# --- Constantes do Protocolo ---

# UPDATE_INTERVAL: A frequência (em segundos) com que o roteador envia as suas
# atualizações de tabela para os vizinhos.
UPDATE_INTERVAL = 10

# TIMEOUT_INTERVAL: O tempo (em segundos) que um roteador espera sem receber
# notícias de um vizinho antes de o considerar offline.
TIMEOUT_INTERVAL = 30

# INFINITY: Um valor de custo que representa uma rota inalcançável.
# Baseado no RIP, que usa 16, mas adaptado para a nossa métrica de custo maior.
INFINITY = 999

# HOLD_DOWN_INTERVAL: O tempo (em segundos) que um roteador ignora novas
# informações sobre uma rota que acabou de falhar, para garantir a estabilidade da rede.
HOLD_DOWN_INTERVAL = 60

# --- Configuração do Logging ---
# Define um formato de log que inclui o nome do logger, para que nos logs do
# docker-compose seja fácil identificar qual roteador gerou a mensagem.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - [%(name)s] - %(message)s",
    datefmt="%H:%M:%S"
)

# --- Funções Auxiliares para Manipular Rotas do S.O. (Linux) ---

def _run_ip_command(arguments: list[str], logger):
    """
    Executa um comando 'ip route' no shell do sistema operacional.

    Esta é uma função auxiliar interna que encapsula a interação com o S.O.,
    incluindo o tratamento de erros comuns.

    Args:
        arguments (list[str]): Uma lista de argumentos a serem passados para 'ip route'.
        logger: A instância do logger do roteador para registar os resultados.
    """
    cmd = ["ip", "route", *arguments]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=5)
        logger.debug("Comando executado: %s", " ".join(cmd))
    except FileNotFoundError:
        logger.error("Comando 'ip' não encontrado. Este script deve rodar num container Linux.")
    except subprocess.CalledProcessError:
        # Ignora erros comuns como "rota não existe ao tentar apagar" ou
        # "rota já existe ao tentar adicionar", que são esperados durante a convergência.
        pass
    except Exception as exc:
        logger.error(f"Erro inesperado ao executar '{' '.join(cmd)}': {exc}")

def add_route(destination_prefix: str, next_hop_ip: str, logger):
    """
    Adiciona ou substitui uma rota na tabela de roteamento do Kernel.

    Usa o comando 'ip route replace' que é idempontente: funciona para adicionar uma
    rota nova ou para modificar uma já existente.

    Args:
        destination_prefix (str): A rede de destino (ex: "10.0.1.0/24").
        next_hop_ip (str): O endereço IP do próximo salto.
        logger: A instância do logger do roteador.
    """
    logger.info(f"SINC S.O.: Adicionando/Trocando rota para {destination_prefix} via {next_hop_ip}")
    _run_ip_command(["replace", destination_prefix, "via", next_hop_ip], logger)

def delete_route(destination_prefix: str, logger):
    """
    Remove uma rota da tabela de roteamento do Kernel.

    Args:
        destination_prefix (str): A rede de destino a ser removida.
        logger: A instância do logger do roteador.
    """
    logger.info(f"SINC S.O.: Removendo rota para {destination_prefix}")
    _run_ip_command(["del", destination_prefix], logger)

# --- Classe Principal do Roteador ---

class SimpleRouter:
    """
    Encapsula o estado e a lógica de um roteador que executa um protocolo
    de roteamento dinâmico do tipo Vetor de Distância.
    """
    def __init__(self, config_path: Path):
        """
        Inicializa o roteador a partir de um arquivo de configuração JSON.

        Args:
            config_path (Path): O caminho para o arquivo de configuração.
        """
        with config_path.open("r") as f:
            config = json.load(f)

        # --- Estado Fundamental ---
        self.router_id = config["router_id"]  # O nome deste roteador (ex: "r1")
        self.logger = logging.getLogger(self.router_id) # Logger específico para esta instância
        self.network_map = config["network_map"] # Mapeamento de IDs para prefixos de rede

        # --- Estado da Rede ---
        # Dicionário com informações sobre os vizinhos diretos
        self.neighbors = {}
        # Dicionário com a tabela de roteamento calculada
        self.routing_table = {self.router_id: {"cost": 0, "next_hop": self.router_id}}
        # Dicionário que armazena as rotas que este script instalou no S.O.
        self.installed_routes = {}
        # Dicionário para rastrear rotas em hold-down para evitar instabilidade
        self.hold_down_timers = {}

        # --- Configuração de Rede ---
        self.listen_ip = "0.0.0.0" # Ouve em todas as interfaces
        self.listen_port = config["listen_port"]
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.listen_ip, self.listen_port))
        self.sock.settimeout(1.0) # Evita que o loop principal bloqueie para sempre

        # --- Controlo de Temporizadores ---
        self.last_update_sent = 0.0 # Hora do último envio de atualização

        # --- Lógica de Inicialização de Custos ---
        # Passo 1: Construir o dicionário de vizinhos primeiro, sem o custo.
        # Isto é necessário para que o cálculo do custo de congestão (len(self.neighbors))
        # seja consistente e não dependa da ordem dos vizinhos no arquivo JSON.
        for neighbor in config["neighbors"]:
            neighbor_id = neighbor["id"]
            self.neighbors[neighbor_id] = {
                "ip": neighbor["ip"],
                "port": neighbor["port"],
                "metrics": neighbor["metrics"],
                "last_seen": 0.0,
            }

        # Passo 2: Agora que self.neighbors está completo, podemos calcular o custo
        # de cada link de forma consistente.
        for neighbor_id in self.neighbors:
            metrics = self.neighbors[neighbor_id]["metrics"]
            self.neighbors[neighbor_id]["cost"] = self._calculate_composite_cost(metrics)
        
        self.logger.info(f"Roteador iniciado. Ouvindo em {self.listen_ip}:{self.listen_port}")

    def _calculate_composite_cost(self, metrics: dict) -> float:
        """
        Calcula um custo composto com base em várias métricas.
        Esta é a "inteligência" customizada do nosso protocolo.
        """
        # Métrica 1: Latência (quanto maior, pior o custo). Padrão de 500ms se não definida.
        latency = metrics.get("latency_ms", 500)
        
        # Métrica 2: Largura de Banda (quanto maior, melhor, por isso usamos o inverso).
        bandwidth = metrics.get("bandwidth_mbps", 1)
        bandwidth_cost = 1000 / bandwidth
        
        # Métrica 3: Congestão (penaliza links originados em roteadores muito conectados).
        congestion_cost = len(self.neighbors) * 0.5

        # Fórmula final que combina todas as métricas num único valor de custo.
        return latency + bandwidth_cost + congestion_cost

    def send_routing_updates(self):
        """
        Envia a tabela de roteamento para cada vizinho, aplicando a regra do
        Split Horizon com Poison Reverse para evitar loops de roteamento.
        """
        # Itera por cada vizinho para lhe enviar uma tabela de roteamento customizada.
        for neighbor_id in self.neighbors.keys():
            table_for_neighbor = {}
            # Itera pela nossa tabela de roteamento principal para decidir o que enviar.
            for dest, route_info in self.routing_table.items():
                # A REGRA DO SPLIT HORIZON COM POISON REVERSE:
                # Se o próximo salto para um destino é o próprio vizinho para quem estamos
                # a enviar a mensagem, anunciamos essa rota de volta, mas com um custo infinito.
                # Isto avisa o vizinho para nunca tentar usar-nos como um caminho de volta.
                if dest != self.router_id and route_info.get("next_hop") == neighbor_id:
                    table_for_neighbor[dest] = {"cost": INFINITY, "next_hop": route_info["next_hop"]}
                else:
                    table_for_neighbor[dest] = route_info
            
            message = {"type": "update", "sender_id": self.router_id, "table": table_for_neighbor}
            payload = json.dumps(message).encode("utf-8")
            try:
                self.sock.sendto(payload, (self.neighbors[neighbor_id]["ip"], self.neighbors[neighbor_id]["port"]))
            except OSError:
                pass # Ignora erros se o socket estiver ocupado ou o vizinho não for alcançável
        self.last_update_sent = time.time()

    def process_incoming_message(self, payload: bytes, source_address: tuple) -> bool:
        """
        Processa uma atualização de tabela recebida de um vizinho, aplicando as
        regras de atualização do algoritmo de Vetor de Distância.
        
        Returns:
            bool: True se a tabela de roteamento foi alterada, False caso contrário.
        """
        try:
            message = json.loads(payload.decode("utf-8"))
            sender_id = message["sender_id"]
            neighbor_table = message["table"]
        except (json.JSONDecodeError, KeyError):
            self.logger.warning(f"Pacote malformado recebido de {source_address}")
            return False
        if sender_id not in self.neighbors:
            return False
        
        # Atualiza o timestamp do vizinho, provando que ele está online.
        self.neighbors[sender_id]["last_seen"] = time.time()
        table_changed = False
        cost_to_neighbor = self.neighbors[sender_id]["cost"]

        # Itera por cada rota anunciada pelo vizinho.
        for destination, info in neighbor_table.items():
            # --- LÓGICA DE HOLD-DOWN TIMER ---
            # Se a rota está em "hold-down", ignoramos completamente a atualização
            # para dar tempo à "má notícia" de se propagar pela rede.
            if destination in self.hold_down_timers:
                if time.time() - self.hold_down_timers[destination] < HOLD_DOWN_INTERVAL:
                    continue
                else:
                    # O timer expirou, podemos voltar a considerar atualizações para este destino.
                    del self.hold_down_timers[destination]
            
            # --- LÓGICA DE SPLIT HORIZON (RECEBIMENTO) ---
            # Ignora rotas que o vizinho está a tentar aprender de nós.
            if info.get("next_hop") == self.router_id:
                continue

            new_cost = cost_to_neighbor + info["cost"]
            current_route = self.routing_table.get(destination)

            # CASO 1: Não conhecemos este destino. Se a rota for válida, aprendemos.
            if not current_route:
                if new_cost < INFINITY:
                    self.routing_table[destination] = {"cost": new_cost, "next_hop": sender_id}
                    table_changed = True
                continue

            # CASO 2: A atualização veio do nosso 'guia' atual (next_hop) para este destino.
            # Confiamos sempre nele, quer a notícia seja boa (custo menor) ou má (custo maior/infinito).
            if current_route.get("next_hop") == sender_id:
                if current_route["cost"] != new_cost:
                    current_route["cost"] = new_cost
                    table_changed = True
            
            # CASO 3: A atualização veio de outro roteador.
            # Só aceitamos a sua palavra se o caminho que ele oferece for estritamente melhor.
            elif new_cost < current_route["cost"]:
                self.routing_table[destination] = {"cost": new_cost, "next_hop": sender_id}
                table_changed = True
                
        return table_changed

    def check_neighbor_timeouts(self) -> bool:
        """
        Verifica se algum vizinho ficou offline (timeout), envenena as rotas que
        dependiam dele e inicia os timers de Hold-Down.
        """
        table_changed = False
        now = time.time()
        for neighbor_id in self.neighbors.keys():
            # A condição verifica se já recebemos alguma mensagem deste vizinho e se o tempo
            # desde a última mensagem é maior que o intervalo de timeout.
            if self.neighbors[neighbor_id]["last_seen"] > 0 and now - self.neighbors[neighbor_id]["last_seen"] > TIMEOUT_INTERVAL:
                self.logger.info(f"TIMEOUT! Vizinho {neighbor_id} parece estar offline.")
                for dest, info in self.routing_table.items():
                    # Para cada rota na nossa tabela que usava o vizinho morto como próximo salto...
                    if info.get("next_hop") == neighbor_id and info.get("cost", 0) < INFINITY:
                        # ...envenenamos a rota e iniciamos o timer de hold-down.
                        self.logger.info(f"Envenenando rota para {dest} e iniciando Hold-Down.")
                        info["cost"] = INFINITY
                        self.hold_down_timers[dest] = now
                        table_changed = True
                # Resetamos o timestamp para não disparar o timeout repetidamente.
                self.neighbors[neighbor_id]["last_seen"] = 0
        return table_changed

    def print_routing_table(self):
        """
        Imprime a tabela de roteamento formatada para o log, omitindo rotas
        inválidas (com custo infinito) para uma visualização mais limpa.
        """
        table_str = "\n" + "="*55 + f"\nTabela de Roteamento em {time.strftime('%H:%M:%S')}\n" + "="*55 + "\n"
        table_str += f"{'Destino':<10} | {'Custo':<10} | {'Próximo Salto':<15}\n" + "-"*55 + "\n"
        
        # Cria uma cópia temporária da tabela, incluindo apenas as rotas válidas.
        valid_routes = {dest: info for dest, info in self.routing_table.items() if info.get("cost", 0) < INFINITY}
        
        if not valid_routes:
            table_str += " (Nenhuma rota válida conhecida)\n"
            
        # Itera e imprime apenas as rotas válidas.
        for dest, info in sorted(valid_routes.items()):
            table_str += f"{dest:<10} | {info['cost']:<10.2f} | {info['next_hop']:<15}\n"
        table_str += "="*55
        self.logger.info(table_str)

    def sync_os_routes(self):
        """
        Sincroniza a tabela de roteamento lógica com a do sistema operacional,
        adicionando rotas válidas e removendo as inválidas (envenenadas).
        """
        # Adiciona ou atualiza rotas válidas.
        for dest_id, route_info in self.routing_table.items():
            if dest_id == self.router_id: continue
            destination_prefix = self.network_map.get(dest_id)
            if not destination_prefix: continue

            if route_info["cost"] >= INFINITY:
                # Se a rota está envenenada, garantimos que ela seja removida do S.O.
                if destination_prefix in self.installed_routes:
                    delete_route(destination_prefix, self.logger)
                    del self.installed_routes[destination_prefix]
            else:
                # Se a rota é válida, garantimos que ela esteja instalada e correta.
                next_hop_id = route_info.get("next_hop")
                next_hop_ip = self.neighbors.get(next_hop_id, {}).get("ip")
                if not next_hop_ip: continue
                if self.installed_routes.get(destination_prefix) != next_hop_ip:
                    add_route(destination_prefix, next_hop_ip, self.logger)
                    self.installed_routes[destination_prefix] = next_hop_ip
        
        # Limpeza final: remove do S.O. quaisquer rotas que já não existem de todo na tabela lógica.
        current_valid_prefixes = {self.network_map.get(dest_id) for dest_id, info in self.routing_table.items() if info["cost"] < INFINITY}
        for installed_prefix in list(self.installed_routes.keys()):
            if installed_prefix not in current_valid_prefixes:
                delete_route(installed_prefix, self.logger)
                del self.installed_routes[installed_prefix]

    def run(self):
        """
        O loop principal do roteador, que orquestra todas as operações.
        """
        self.print_routing_table()
        self.sync_os_routes()
        
        while True:
            # A cada ciclo do loop, o roteador realiza as suas três tarefas principais:
            
            # Tarefa 1: Enviar atualizações periódicas para os vizinhos.
            if time.time() - self.last_update_sent > UPDATE_INTERVAL:
                self.send_routing_updates()
            
            # Tarefa 2: Processar mensagens recebidas da rede.
            table_changed_by_message = False
            try:
                payload, addr = self.sock.recvfrom(4096)
                table_changed_by_message = self.process_incoming_message(payload, addr)
            except socket.timeout:
                pass # É normal não receber pacotes em todos os ciclos.
            except ConnectionResetError:
                pass # É normal se um vizinho for desligado abruptamente.
            
            # Tarefa 3: Verificar se algum vizinho ficou offline.
            table_changed_by_timeout = self.check_neighbor_timeouts()
            
            # Se a tabela foi alterada por qualquer motivo, imprimimos e sincronizamos.
            if table_changed_by_message or table_changed_by_timeout:
                self.print_routing_table()
                self.sync_os_routes()
            
            # Pausa para evitar consumo excessivo de CPU.
            time.sleep(0.1)

def main():
    """
    Ponto de entrada principal da aplicação.
    
    Analisa os argumentos da linha de comando, cria e inicia a instância do roteador.
    """
    parser = argparse.ArgumentParser(description="Simple Distance-Vector Router")
    parser.add_argument("--config", type=Path, required=True, help="Caminho para o arquivo de configuração JSON")
    args = parser.parse_args()
    
    router = SimpleRouter(args.config)
    # Define o nome do logger principal para o ID do roteador para logs mais claros
    logging.getLogger().name = router.router_id
    router.run()

if __name__ == "__main__":
    main()