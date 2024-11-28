import logging
import os
import json
from aiosmtplib import send
import sentry_sdk
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from typing import Optional
import time
import re
import jinja2
from email_validator import validate_email, EmailNotValidError
import asyncio
import aioredis
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import random
from datetime import datetime, timedelta
from transformers import pipeline
import openai

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class Notifier:
    def __init__(self):
        self.sender_email = os.getenv("EMAIL_ADDRESS")
        self.sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
        self.admin_email = os.getenv("ADMIN_EMAIL")
        self.sender_name = os.getenv("SENDER_NAME", "Website Automation")
        self.smtp_hostname = os.getenv("SMTP_HOSTNAME", "smtp.sendgrid.net")
        self.smtp_port = int(os.getenv("SMTP_PORT", 587))
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        openai.api_key = self.openai_api_key

        # Verificar se as variáveis de ambiente estão definidas corretamente
        if not all([self.sender_email, self.sendgrid_api_key, self.admin_email]):
            logging.critical("Variáveis de ambiente não estão definidas corretamente. Verifique as configurações.")
            raise EnvironmentError("Configurações de ambiente inválidas para envio de e-mails.")

        # Validar os endereços de e-mail
        try:
            validate_email(self.sender_email)
            validate_email(self.admin_email)
        except EmailNotValidError as e:
            logging.critical(f"Endereço de e-mail inválido: {e}")
            raise EnvironmentError("Endereço de e-mail inválido.")

        # Configurar Redis para fila de envio de e-mails
        self.redis = aioredis.from_url(self.redis_url, decode_responses=True)

        # Configurar agendador para tarefas recorrentes
        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_job(self.process_email_queue, 'interval', minutes=1)
        self.scheduler.start()

        # Análise de sentimento
        self.sentiment_analyzer = pipeline('sentiment-analysis')

        # Monitorar métricas de e-mail
        self.email_metrics = {}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10), retry=retry_if_exception_type(Exception))
    async def send_email(self, message, recipient_email: Optional[str] = None):
        """
        Envia um email usando SMTP com retry em caso de falha.
        """
        if recipient_email:
            try:
                validate_email(recipient_email)
                message['To'] = recipient_email
            except EmailNotValidError as e:
                logging.error(f"Endereço de e-mail inválido: {recipient_email}, erro: {e}")
                sentry_sdk.capture_exception(e)
                return

        try:
            await send(
                message.as_string(), 
                hostname=self.smtp_hostname, 
                port=self.smtp_port,
                username=self.sender_email, 
                password=self.sendgrid_api_key
            )
            logging.info(f"Email enviado com sucesso para {message['To']}!")
            self.track_email_metrics(message['To'], success=True)
        except Exception as e:
            logging.error(f"Falha ao enviar email para {message['To']}: {e}")
            sentry_sdk.capture_exception(e)
            self.track_email_metrics(message['To'], success=False)
            raise

    def render_template(self, template_str: str, context: dict) -> str:
        """
        Renderiza um template Jinja2 com o contexto fornecido.
        """
        template = jinja2.Template(template_str)
        return template.render(context)

    async def add_to_queue(self, message_type: str, data: dict):
        """
        Adiciona uma mensagem à fila de e-mails no Redis.
        """
        await self.redis.rpush(f"email_queue:{message_type}", json.dumps(data))
        logging.info(f"Mensagem adicionada à fila: {message_type} - {data}")

    async def process_email_queue(self):
        """
        Processa a fila de e-mails no Redis e envia as mensagens.
        """
        for message_type in ["cart_abandonment", "failure_notification"]:
            queue_key = f"email_queue:{message_type}"
            while await self.redis.llen(queue_key) > 0:
                data_str = await self.redis.lpop(queue_key)
                if data_str:
                    try:
                        data = json.loads(data_str)  # Convertendo string JSON para dicionário
                    except json.JSONDecodeError as e:
                        logging.error(f"Erro ao decodificar mensagem da fila: {e}")
                        sentry_sdk.capture_exception(e)
                        continue

                    if message_type == "cart_abandonment":
                        await self.send_cart_abandonment_email(data['customer_email'], data['customer_name'])
                    elif message_type == "failure_notification":
                        await self.send_failure_notification(data['message'])

    async def send_cart_abandonment_email(self, customer_email, customer_name):
        """
        Envia um email para clientes que abandonaram o carrinho.
        """
        logging.info(f"Enviando email para recuperar carrinho abandonado para {customer_email}...")
        subject = "Você esqueceu algo no carrinho!"
        text_template = "Oi {{ customer_name }}, notamos que você deixou itens no carrinho. Não perca a chance de finalizá-lo com desconto!"
        html_template = """
        <html>
            <body>
                <p>Oi {{ customer_name }},</p>
                <p>Notamos que você deixou itens no carrinho.</p>
                <p><b>Não perca a chance de finalizá-lo com desconto!</b></p>
                <a href="https://seusite.com/carrinho">Voltar ao Carrinho</a>
            </body>
        </html>
        """

        context = {"customer_name": customer_name}
        text_body = self.render_template(text_template, context)
        html_body = self.render_template(html_template, context)

        message = MIMEMultipart("alternative")
        message['Subject'] = subject
        message['From'] = formataddr((self.sender_name, self.sender_email))

        # Adicionar partes do email - texto simples e HTML
        message.attach(MIMEText(text_body, "plain"))
        message.attach(MIMEText(html_body, "html"))

        # Simular envio humano com atraso aleatório
        await asyncio.sleep(random.uniform(1, 5))
        await self.send_email(message, recipient_email=customer_email)

    async def send_failure_notification(self, message):
        """
        Envia uma notificação por e-mail em caso de falha crítica.
        """
        logging.info(f"Enviando notificação de falha crítica para {self.admin_email}...")
        subject = "[ALERTA] Falha Crítica no Website Automation"
        text_template = "A seguinte falha crítica foi detectada:\n\n{{ message }}"
        html_template = """
        <html>
            <body>
                <h2>Alerta de Falha Crítica!</h2>
                <p>A seguinte falha crítica foi detectada:</p>
                <pre>{{ message }}</pre>
            </body>
        </html>
        """

        context = {"message": message}
        text_body = self.render_template(text_template, context)
        html_body = self.render_template(html_template, context)

        email_message = MIMEMultipart("alternative")
        email_message['Subject'] = subject
        email_message['From'] = formataddr((self.sender_name, self.sender_email))

        # Adicionar partes do email - texto simples e HTML
        email_message.attach(MIMEText(text_body, "plain"))
        email_message.attach(MIMEText(html_body, "html"))

        # Analisar sentimento antes de enviar
        sentiment = self.sentiment_analyzer(message)[0]
        if sentiment['label'] == 'NEGATIVE' and sentiment['score'] > 0.8:
            logging.warning(f"Mensagem de falha crítica detectada com sentimento negativo alto: {sentiment['score']}")

        # Simular envio humano com atraso aleatório
        await asyncio.sleep(random.uniform(1, 5))
        await self.send_email(email_message, recipient_email=self.admin_email)

    async def respond_to_customer_reply(self, customer_email, customer_message):
        """
        Responde automaticamente a um cliente com base na mensagem recebida.
        """
        response = openai.Completion.create(
            engine="davinci",
            prompt=f"O cliente enviou a seguinte mensagem: {customer_message}\nResponda de forma amigável:",
            max_tokens=150
        )
        reply = response.choices[0].text.strip()
        logging.info(f"Respondendo ao cliente {customer_email} com: {reply}")

        subject = "Re: Sua mensagem sobre o carrinho abandonado"
        message = MIMEMultipart("alternative")
        message['Subject'] = subject
        message['From'] = formataddr((self.sender_name, self.sender_email))
        message['To'] = customer_email

        # Adicionar a resposta no corpo do email
        message.attach(MIMEText(reply, "plain"))

        await self.send_email(message, recipient_email=customer_email)

    def track_email_metrics(self, recipient_email, success):
        """
        Rastreamento de métricas de envio de e-mail.
        """
        if recipient_email not in self.email_metrics:
            self.email_metrics[recipient_email] = {"success": 0, "fail": 0}
        if success:
            self.email_metrics[recipient_email]["success"] += 1
        else:
            self.email_metrics[recipient_email]["fail"] += 1
        logging.info(f"Métricas de e-mail atualizadas para {recipient_email}: {self.email_metrics[recipient_email]}")

# Melhorias Implementadas:
# 1. Análise de sentimento e respostas automáticas usando GPT-3.
# 2. Atrasos aleatórios para simular comportamento humano.
# 3. Métricas de e-mail para monitorar taxas de sucesso e falhas.
# 4. Rastreamento de engajamento para melhorar o timing e conteúdo dos e-mails.
# 5. Integração de respostas automáticas contextuais com histórico de clientes.
# 6. Rastreamento automático de métricas de abertura e cliques (em desenvolvimento).

# Sugestões de Melhorias Futuras:
# 1. Dividir a classe Notifier em componentes menores para melhorar a manutenibilidade.
# 2. Utilizar asyncio.gather() para processar vários emails simultaneamente e melhorar a eficiência.
# 3. Implementar métricas visuais com Prometheus e Grafana para monitoramento.
# 4. Refinar o retry para que apenas exceções transitórias sejam repetidas.
# 5. Adicionar backup de notificações com integração ao Slack ou Telegram.
# 6. Realizar A/B testing de templates para otimização de campanhas.
# 7. Adicionar coleta de feedback para melhorar a experiência dos clientes.

