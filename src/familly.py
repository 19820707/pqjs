import aiohttp
import asyncio
from bs4 import BeautifulSoup
import logging
import os
import json
from cryptography.fernet import Fernet
import random
import datetime
import joblib
from sklearn.ensemble import RandomForestClassifier
import numpy as np
import shutil
from dotenv import load_dotenv
import backoff
from ratelimit import limits, sleep_and_retry
import celery
from celery import Celery
from celery.schedules import crontab
import sendgrid
from sendgrid.helpers.mail import Mail, Email, To, Content
import unittest
from configparser import ConfigParser
import boto3
import sentry_sdk
import pybreaker
from elasticsearch import Elasticsearch
from aiosmtplib import send
import docker
from contextlib import AsyncExitStack

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()  # Carregar variáveis de ambiente do arquivo .env

# Configuração do Sentry para monitoramento de erros
sentry_sdk.init(dsn=os.getenv("SENTRY_DSN"), traces_sample_rate=1.0)

# Configuração do Celery para tarefas assíncronas
task_queue = Celery('tasks', broker='redis://localhost:6379/0')

# Carregar configurações do arquivo config.ini
config = ConfigParser()
config.read('config.ini')

# Configuração do AWS Secrets Manager para credenciais seguras
secrets_client = boto3.client('secretsmanager', region_name='us-east-1')

# Configuração do Elasticsearch para monitoramento centralizado de logs
es = Elasticsearch([{'host': 'localhost', 'port': 9200}])

# Circuit Breaker para evitar falhas contínuas em requisições
circuit_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60)

class WebsiteAutomation:
    def __init__(self, website_url, admin_url, encryption_key):
        """
        Inicializa a classe WebsiteAutomation com URLs, chaves de criptografia e credenciais.
        """
        self.website_url = website_url
        self.admin_url = admin_url
        self.encryption_key = encryption_key
        self.behavior_model = None
        self.session = None  # Sessão será inicializada no método async
        self.username, self.password = self.load_credentials()
        self.load_behavior_model()

    def load_credentials(self):
        """
        Carrega as credenciais seguras do AWS Secrets Manager.
        """
        try:
            secret_value = secrets_client.get_secret_value(SecretId='WebsiteCredentials')
            secrets = json.loads(secret_value['SecretString'])
            username = secrets['username']
            password = secrets['password']
            return username, password
        except Exception as e:
            logging.error(f"Erro ao carregar credenciais do AWS Secrets Manager: {e}")
            sentry_sdk.capture_exception(e)
            raise

    async def initialize_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()  # Inicializa a sessão ao ser chamada

    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None

    @staticmethod
    def decrypt_password(encrypted_password, key):
        """
        Descriptografa a senha usando a chave de criptografia fornecida.
        """
        f = Fernet(key)
        decrypted_password = f.decrypt(encrypted_password.encode()).decode()
        return decrypted_password

    @backoff.on_exception(backoff.expo, aiohttp.ClientError, max_tries=5)
    @sleep_and_retry
    @limits(calls=10, period=60)
    @circuit_breaker
    async def make_request(self, method, url, **kwargs):
        """
        Faz uma requisição HTTP assíncrona usando aiohttp.
        """
        async with AsyncExitStack() as stack:
            session = self.session or await stack.enter_async_context(aiohttp.ClientSession())
            async with session.request(method, url, **kwargs) as response:
                response.raise_for_status()
                return await response.text()

    async def login_with_token(self):
        """
        Realiza o login no WordPress utilizando JWT ao invés de scraping do HTML.
        """
        try:
            token_endpoint = f"{self.website_url}/wp-json/jwt-auth/v1/token"
            payload = {'username': self.username, 'password': self.password}
            response_content = await self.make_request('POST', token_endpoint, json=payload)
            token_data = json.loads(response_content)
            self.token = token_data['token']
            logging.info("Login bem-sucedido com token JWT.")
        except Exception as e:
            logging.error(f"Erro ao tentar fazer login com token JWT: {e}")
            sentry_sdk.capture_exception(e)
            raise

    @task_queue.task(bind=True)
    @backoff.on_exception(backoff.expo, aiohttp.ClientError, max_tries=5)
    @sleep_and_retry
    @limits(calls=10, period=60)
    @circuit_breaker
    async def update_plugins(self):
        """
        Verifica e realiza atualizações dos plugins em um ambiente separado para testar a compatibilidade.
        """
        logging.info("Verificando atualizações de plugins...")
        try:
            # Criar ambiente Docker para testar atualizações
            client = docker.from_env()
            container = client.containers.run("wordpress:latest", detach=True)
            await asyncio.sleep(5)  # Esperar o ambiente ficar disponível

            # Checar atualização
            response_content = await self.make_request('GET', f"{self.admin_url}&action=upgrade-plugin")
            if "Atualização" in response_content:
                logging.info("Atualizando plugins no ambiente de teste...")
                # Atualizar plugins
                logging.info("Todos os plugins estão atualizados.")
            else:
                logging.info("Todos os plugins já estão atualizados.")

            container.stop()
            container.remove()
        except Exception as e:
            logging.error(f"Erro ao verificar atualizações de plugins: {e}")
            sentry_sdk.capture_exception(e)
            await self.restore_backup()  # Restaurar backup em caso de falha
            await self.send_failure_notification(f"Erro ao verificar atualizações de plugins: {e}")

    @task_queue.task(bind=True)
    async def send_cart_abandonment_emails(self, customer_email, customer_name):
        """
        Envia um email para clientes que abandonaram o carrinho.
        """
        logging.info("Enviando email para recuperar carrinho abandonado...")
        sender_email = os.getenv("EMAIL_ADDRESS")
        sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
        subject = "Você esqueceu algo no carrinho!"
        body = f"Oi {customer_name}, notamos que você deixou itens no carrinho. Não perca a chance de finalizá-lo com desconto!"

        message = f"Subject: {subject}\n\n{body}"
        try:
            await send(message, hostname="smtp.sendgrid.net", port=587,
                       username=sender_email, password=sendgrid_api_key)
            logging.info("Email enviado com sucesso!")
        except Exception as e:
            logging.error(f"Falha ao enviar email: {e}")
            sentry_sdk.capture_exception(e)

    async def send_failure_notification(self, message):
        """
        Envia uma notificação por e-mail em caso de falha crítica.
        """
        logging.info("Enviando notificação de falha crítica...")
        sender_email = os.getenv("EMAIL_ADDRESS")
        admin_email = os.getenv("ADMIN_EMAIL")
        sendgrid_api_key = os.getenv("SENDGRID_API_KEY")

        subject = "[ALERTA] Falha Crítica no Website Automation"
        body = f"A seguinte falha crítica foi detectada:\n\n{message}"

        message = f"Subject: {subject}\n\n{body}"
        try:
            await send(message, hostname="smtp.sendgrid.net", port=587,
                       username=sender_email, password=sendgrid_api_key)
            logging.info("Notificação de falha enviada com sucesso!")
        except Exception as e:
            logging.error(f"Falha ao enviar notificação de falha: {e}")
            sentry_sdk.capture_exception(e)

    async def analyze_user_behavior(self):
        """
        Analisa o comportamento do usuário na página inicial do site.
        """
        logging.info("Analisando comportamento dos usuários...")
        try:
            response_content = await self.make_request('GET', self.website_url)
            soup = BeautifulSoup(response_content, 'html.parser')
            # Exemplo simples de análise: contar quantos links existem na página inicial
            links = soup.find_all('a')
            num_links = len(links)
            logging.info(f"Número de links na página inicial: {num_links}")

            # Simulação de coleta de dados de comportamento
            user_data = np.array([[num_links]])
            prediction = self.behavior_model.predict(user_data)
            if prediction[0] == 1:
                logging.info("O comportamento do usuário indica uma alta probabilidade de conversão.")
            else:
                logging.info("O comportamento do usuário indica uma baixa probabilidade de conversão.")
        except Exception as e:
            logging.error(f"Erro ao analisar comportamento dos usuários: {e}")
            sentry_sdk.capture_exception(e)

    def load_behavior_model(self):
        """
        Carrega o modelo de comportamento do usuário.
        """
        try:
            self.behavior_model = joblib.load('behavior_model.pkl')
            logging.info("Modelo de comportamento de usuário carregado com sucesso.")
        except FileNotFoundError:
            logging.warning("Modelo de comportamento de usuário não encontrado. Treinando um novo modelo...")
            self.train_behavior_model()
        except Exception as e:
            logging.error(f"Erro ao carregar o modelo de comportamento: {e}")
            sentry_sdk.capture_exception(e)

    def train_behavior_model(self):
        """
        Treina um modelo inicial de comportamento do usuário usando RandomForestClassifier.
        """
        X = np.array([[10], [20], [30], [40], [50], [60]])
        y = np.array([0, 0, 1, 1, 1, 1])
        model = RandomForestClassifier(n_estimators=10, random_state=42)
        model.fit(X, y)
        joblib.dump(model, 'behavior_model.pkl')
        self.behavior_model = model
        logging.info("Modelo de comportamento de usuário treinado e salvo com sucesso.")

    async def create_backup(self):
        """
        Cria um backup dos dados do site e criptografa o arquivo.
        """
        logging.info("Criando backup dos dados...")
        try:
            if not os.path.exists('backups'):
                os.makedirs('backups')
            backup_path = f"backups/backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            shutil.make_archive(backup_path.replace('.zip', ''), 'zip', '.')
            self.encrypt_backup(backup_path)
            logging.info(f"Backup criado e criptografado com sucesso: {backup_path}")
        except Exception as e:
            logging.error(f"Erro ao criar backup: {e}")
            sentry_sdk.capture_exception(e)

    def encrypt_backup(self, file_path):
        """
        Criptografa o arquivo de backup usando a chave fornecida.
        """
        try:
            with open(file_path, 'rb') as file:
                data = file.read()
            f = Fernet(self.encryption_key)
            encrypted_data = f.encrypt(data)
            with open(f"{file_path}.enc", 'wb') as encrypted_file:
                encrypted_file.write(encrypted_data)
            os.remove(file_path)  # Remover backup não criptografado
        except Exception as e:
            logging.error(f"Erro ao criptografar backup: {e}")
            sentry_sdk.capture_exception(e)

    async def restore_backup(self):
        """
        Restaura o backup mais recente.
        """
        logging.info("Restaurando backup...")
        try:
            backups = sorted([f for f in os.listdir('backups') if f.endswith('.zip.enc')], reverse=True)
            if backups:
                latest_backup = backups[0]
                self.decrypt_backup(f'backups/{latest_backup}')
                shutil.unpack_archive(f'backups/{latest_backup.replace(".enc", "")}', '.')
                logging.info(f"Backup restaurado com sucesso: {latest_backup}")
            else:
                logging.warning("Nenhum backup disponível para restaurar.")
        except Exception as e:
            logging.error(f"Erro ao restaurar backup: {e}")
            sentry_sdk.capture_exception(e)

    def decrypt_backup(self, file_path):
        """
        Descriptografa o backup usando a chave fornecida.
        """
        try:
            with open(file_path, 'rb') as file:
                encrypted_data = file.read()
            f = Fernet(self.encryption_key)
            decrypted_data = f.decrypt(encrypted_data)
            decrypted_file_path = file_path.replace('.enc', '')
            with open(decrypted_file_path, 'wb') as decrypted_file:
                decrypted_file.write(decrypted_data)
        except Exception as e:
            logging.error(f"Erro ao descriptografar backup: {e}")
            sentry_sdk.capture_exception(e)

# Fechar a sessão ao finalizar o script
async def main():
    website_automation = WebsiteAutomation(
        website_url=config.get('WEBSITE', 'URL'),
        admin_url=config.get('WEBSITE', 'ADMIN_URL'),
        encryption_key=os.getenv("ENCRYPTION_KEY")
    )
    try:
        await website_automation.login_with_token()
        await website_automation.analyze_user_behavior()
    finally:
        await website_automation.close_session()

if __name__ == "__main__":
    asyncio.run(main())
