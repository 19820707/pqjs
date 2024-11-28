import aiohttp
import logging
import backoff
from ratelimit import limits, sleep_and_retry
import pybreaker
from contextlib import AsyncExitStack
import asyncio
from aiohttp import ClientTimeout
from decouple import config
from aiocache import cached, Cache
import prometheus_client
from prometheus_client import Counter, Histogram, Gauge
import time
from typing import Optional
import numpy as np
import random
from sklearn.linear_model import LinearRegression

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Métricas Prometheus
REQUEST_COUNTER = Counter('request_count', 'Número de requisições feitas', ['method', 'endpoint'])
REQUEST_LATENCY = Histogram('request_latency_seconds', 'Tempo de latência das requisições', ['method', 'endpoint'])
ERROR_COUNTER = Counter('request_errors', 'Número de erros em requisições', ['method', 'endpoint', 'status_code'])
SESSION_STATUS = Gauge('session_status', 'Status da sessão HTTP (1 para ativa, 0 para inativa)')

# Circuit Breaker para evitar falhas contínuas em requisições
circuit_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60)

class RequestHandler:
    def __init__(self, timeout=30, max_retries=5, rate_limit_calls=10, rate_limit_period=60):
        self.session = None  # Sessão será inicializada no método async
        self.timeout = ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self.rate_limit_calls = rate_limit_calls
        self.rate_limit_period = rate_limit_period

        # Informações de Login do Website (seguras)
        self.website_url = "https://familyjoy.online/wp-login.php"
        self.website_admin = "https://familyjoy.online/wp-admin/admin.php?page=alids"
        self.username = config('WP_USERNAME')
        self.password = config('WP_PASSWORD')

        # Verificar se as credenciais estão definidas
        if not self.username or not self.password:
            raise ValueError("Credenciais do WordPress não fornecidas corretamente.")

        # Inicializando modelo de regressão linear para previsão de timeout
        self.latency_data = []  # Armazena histórico de latências
        self.model = LinearRegression()

    async def __aenter__(self):
        await self.initialize_session()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            logging.error(f"Erro encontrado ao sair do contexto: {exc_type}, {exc}")
        await self.close_session()

    async def initialize_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=self.timeout)  # Inicializa a sessão ao ser chamada
            SESSION_STATUS.set(1)
            logging.info("Sessão HTTP inicializada.")

    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None
            SESSION_STATUS.set(0)
            logging.info("Sessão HTTP encerrada.")

    async def auto_reinitialize_session(self):
        """
        Reinicializa a sessão automaticamente em caso de falhas repetitivas.
        """
        logging.warning("Reinicializando sessão HTTP...")
        await self.close_session()
        await self.initialize_session()

    def update_latency_model(self, latency):
        """
        Atualiza o modelo de regressão linear com novos dados de latência.
        """
        self.latency_data.append(latency)
        if len(self.latency_data) > 10:  # Treina o modelo após 10 dados coletados
            X = np.array(range(len(self.latency_data))).reshape(-1, 1)
            y = np.array(self.latency_data).reshape(-1, 1)
            self.model.fit(X, y)
            logging.info("Modelo de previsão de latência atualizado.")

    def predict_timeout(self):
        """
        Previsão do próximo timeout com base no modelo de regressão linear.
        """
        if len(self.latency_data) >= 10:
            next_index = np.array([[len(self.latency_data)]])
            predicted_latency = self.model.predict(next_index)[0][0]
            return min(max(predicted_latency * 2, 5), 60)  # Limita o timeout entre 5 e 60 segundos
        return self.timeout.total  # Retorna o timeout padrão se não houver dados suficientes

    @backoff.on_exception(backoff.expo, (aiohttp.ClientConnectionError, asyncio.TimeoutError), max_tries=lambda self: self.max_retries)
    @sleep_and_retry
    @limits(calls=lambda self: self.rate_limit_calls, period=lambda self: self.rate_limit_period)
    @circuit_breaker
    @cached(ttl=300, cache=Cache.MEMORY)
    async def make_request(self, method, url, timeout=None, **kwargs):
        """
        Faz uma requisição HTTP assíncrona usando aiohttp.
        """
        valid_methods = {'GET', 'POST', 'PUT', 'DELETE', 'PATCH'}
        if method.upper() not in valid_methods:
            raise ValueError(f"Método HTTP inválido: {method}")

        REQUEST_COUNTER.labels(method=method.upper(), endpoint=url).inc()
        predicted_timeout = self.predict_timeout() if timeout is None else timeout
        logging.info(f"Usando timeout previsto: {predicted_timeout} segundos para {url}")
        start_time = time.time()
        with REQUEST_LATENCY.labels(method=method.upper(), endpoint=url).time():
            async with AsyncExitStack() as stack:
                session = self.session or await stack.enter_async_context(aiohttp.ClientSession(timeout=ClientTimeout(total=predicted_timeout)))
                try:
                    async with session.request(method.upper(), url, **kwargs) as response:
                        latency = time.time() - start_time
                        self.update_latency_model(latency)
                        if response.status in [401, 403, 404]:
                            ERROR_COUNTER.labels(method=method.upper(), endpoint=url, status_code=response.status).inc()
                            logging.error(f"Erro {response.status}: {response.reason} para {url}")
                        response.raise_for_status()
                        content_type = response.headers.get('Content-Type', '')
                        if 'application/json' in content_type:
                            return await response.json()
                        return await response.text()
                except aiohttp.ClientResponseError as e:
                    ERROR_COUNTER.labels(method=method.upper(), endpoint=url, status_code=e.status).inc()
                    logging.error(f"Erro ao fazer requisição para {url} (resposta do servidor): {e}")
                    if e.status in [500, 502, 503, 504]:
                        await self.auto_reinitialize_session()
                    raise
                except Exception as e:
                    logging.error(f"Erro ao fazer requisição para {url}: {e}")
                    await self.auto_reinitialize_session()
                    raise

    async def make_multiple_requests(self, requests):
        """
        Faz múltiplas requisições HTTP assíncronas em paralelo.
        """
        tasks = [self.make_request(req['method'], req['url'], timeout=req.get('timeout'), **req.get('kwargs', {})) for req in requests]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Processa as respostas para identificar erros e retornar uma estrutura mais organizada
        result = [
            {"url": req['url'], "response": response} if not isinstance(response, Exception) else {"url": req['url'], "error": str(response)}
            for req, response in zip(requests, responses)
        ]
        for entry in result:
            if "error" in entry:
                logging.error(f"Falha na requisição para {entry['url']}: {entry['error']}")
            else:
                logging.info(f"Resposta da requisição para {entry['url']}: {str(entry['response'])[:100]}...")
        
        return result

    async def login_wordpress(self):
        """
        Faz login no WordPress usando as credenciais fornecidas.
        """
        payload = {
            'log': self.username,
            'pwd': self.password,
            'wp-submit': 'Log In',
            'redirect_to': self.website_admin,
            'testcookie': '1'
        }
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        try:
            response = await self.make_request("POST", self.website_url, data=payload, headers=headers)
            if 'Invalid login' in response:
                logging.error("Credenciais inválidas fornecidas.")
                return None
            logging.info("Login bem-sucedido no WordPress.")
            return response
        except aiohttp.ClientResponseError as e:
            ERROR_COUNTER.labels(method="POST", endpoint=self.website_url, status_code=e.status).inc()
            logging.error(f"Erro ao fazer login no WordPress (resposta do servidor): {e}")
            await self.auto_reinitialize_session()
        except Exception as e:
            logging.error(f"Falha ao fazer login no WordPress: {e}")
            await self.auto_reinitialize_session()
            return None

# Exemplo de uso da classe aprimorada
async def main():
    async with RequestHandler(timeout=30, max_retries=3, rate_limit_calls=5, rate_limit_period=30) as handler:
        # Fazer login no WordPress
        await handler.login_wordpress()

        # Realizar múltiplas requisições
        requests = [
            {"method": "GET", "url": "https://api.example.com/data/1"},
            {"method": "GET", "url": "https://api.example.com/data/2"},
            {"method": "GET", "url": "https://api.example.com/invalid"}  # URL para simular erro
        ]
        responses = await handler.make_multiple_requests(requests)
        for response in responses:
            if "error" in response:
                logging.error(f"Erro ao acessar {response['url']}: {response['error']}")
            else:
                logging.info(f"Conteúdo recebido de {response['url']}: {str(response['response'])[:100]}...")

if __name__ == "__main__":
    prometheus_client.start_http_server(8000)  # Iniciar servidor Prometheus para monitoramento
    asyncio.run(main())


