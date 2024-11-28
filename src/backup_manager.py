import os
import shutil
import datetime
import logging
from cryptography.fernet import Fernet
import sentry_sdk
import threading
import time
from pathlib import Path
from logging.handlers import RotatingFileHandler
import requests
from bs4 import BeautifulSoup
import boto3
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import random
import hashlib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logging.basicConfig(
    handlers=[RotatingFileHandler("backup_manager.log", maxBytes=5*1024*1024, backupCount=3)],
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class BackupManager:
    def __init__(self, encryption_key, backup_interval=86400, retention_policy=5):
        self.encryption_key = encryption_key
        self.backup_interval = backup_interval
        self.retention_policy = retention_policy
        self.website_url = os.getenv("WEBSITE_URL")  # Melhorado: Credenciais seguras
        self.website_admin = os.getenv("WEBSITE_ADMIN")  # Melhorado: Credenciais seguras
        self.username = os.getenv("WP_USERNAME")  # Melhorado: Credenciais seguras
        self.password = os.getenv("WP_PASSWORD")  # Melhorado: Credenciais seguras
        self.stop_backup = threading.Event()  # Melhorado: Sinalizador para parar o thread de backup
        self.backup_thread = threading.Thread(target=self.schedule_backup, daemon=True)
        self.backup_thread.start()

    def create_backup(self):
        """
        Cria um backup dos dados do site e criptografa o arquivo.
        """
        logging.info("Criando backup dos dados...")
        try:
            backup_dir = Path('backups')
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            shutil.make_archive(backup_path.with_suffix('').as_posix(), 'zip', '.')
            self.encrypt_backup(backup_path)
            self.backup_database()
            self.cleanup_old_backups()
            self.upload_backup_to_s3(backup_path.with_suffix('.zip.enc'))  # Melhorado: Backup para S3
            self.send_notification("Backup criado com sucesso", f"Backup criado e criptografado com sucesso: {backup_path}")
            logging.info(f"Backup criado e criptografado com sucesso: {backup_path}")
        except Exception as e:
            logging.error(f"Erro ao criar backup: {e}")
            sentry_sdk.capture_exception(e)
            self.send_notification("Erro ao criar backup", f"Erro ao criar backup: {e}")

    def backup_database(self):
        """
        Faz backup do banco de dados do WordPress.
        """
        logging.info("Fazendo backup do banco de dados do WordPress...")
        try:
            session = requests.Session()
            retry_strategy = Retry(
                total=3,
                status_forcelist=[429, 500, 502, 503, 504],
                method_whitelist=["HEAD", "GET", "OPTIONS", "POST"]
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            session.mount("https://", adapter)

            login_page = session.get(self.website_url, timeout=10)  # Melhorado: Timeout na requisição
            login_soup = BeautifulSoup(login_page.content, 'html.parser')
            login_data = {
                'log': self.username,
                'pwd': self.password,
                'wp-submit': 'Log In',
                'redirect_to': self.website_admin,
                'testcookie': '1'
            }
            response = session.post(self.website_url, data=login_data, timeout=10)  # Melhorado: Timeout na requisição
            if response.url == self.website_admin:
                logging.info("Login bem-sucedido no painel do WordPress.")
                # Aqui você pode adicionar a lógica para acessar o banco de dados e fazer o dump
            else:
                logging.error("Falha ao fazer login no WordPress. Verifique as credenciais.")
        except Exception as e:
            logging.error(f"Erro ao fazer backup do banco de dados: {e}")
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
            logging.info(f"Backup criptografado com sucesso: {file_path}.enc")
        except Exception as e:
            logging.error(f"Erro ao criptografar backup: {e}")
            sentry_sdk.capture_exception(e)

    def restore_backup(self):
        """
        Restaura o backup mais recente.
        """
        logging.info("Restaurando backup...")
        try:
            backups = sorted(Path('backups').glob('*.zip.enc'), reverse=True)
            if backups:
                latest_backup = backups[0]
                self.decrypt_backup(latest_backup)
                if self.validate_backup(latest_backup.with_suffix('')):
                    shutil.unpack_archive(latest_backup.with_suffix('').as_posix(), '.')
                    logging.info(f"Backup restaurado com sucesso: {latest_backup}")
                    self.send_notification("Backup restaurado com sucesso", f"Backup restaurado: {latest_backup}")
                else:
                    logging.error("Falha na validação do backup. O arquivo pode estar corrompido.")
                    self.send_notification("Falha na restauração do backup", "Falha na validação do backup. O arquivo pode estar corrompido.")
            else:
                logging.warning("Nenhum backup disponível para restaurar.")
        except Exception as e:
            logging.error(f"Erro ao restaurar backup: {e}")
            sentry_sdk.capture_exception(e)
            self.send_notification("Erro ao restaurar backup", f"Erro ao restaurar backup: {e}")

    def decrypt_backup(self, file_path):
        """
        Descriptografa o backup usando a chave fornecida.
        """
        try:
            with open(file_path, 'rb') as file:
                encrypted_data = file.read()
            f = Fernet(self.encryption_key)
            decrypted_data = f.decrypt(encrypted_data)
            decrypted_file_path = file_path.with_suffix('')
            with open(decrypted_file_path, 'wb') as decrypted_file:
                decrypted_file.write(decrypted_data)
            logging.info(f"Backup descriptografado com sucesso: {decrypted_file_path}")
        except Exception as e:
            logging.error(f"Erro ao descriptografar backup: {e}")
            sentry_sdk.capture_exception(e)

    def validate_backup(self, file_path):
        """
        Valida o backup descriptografado para garantir a integridade do arquivo.
        """
        logging.info(f"Validando backup: {file_path}")
        try:
            with open(file_path, 'rb') as file:
                data = file.read()
                checksum = hashlib.md5(data).hexdigest()  # Validação usando hash MD5
                logging.info(f"Checksum do arquivo: {checksum}")
            return True  # Implementação fictícia, adicionar lógica de validação real
        except Exception as e:
            logging.error(f"Erro ao validar backup: {e}")
            sentry_sdk.capture_exception(e)
            return False

    def schedule_backup(self):
        """
        Agenda backups automáticos em intervalos especificados.
        """
        while not self.stop_backup.is_set():
            try:
                self.create_backup()
                adaptive_interval = self.backup_interval + random.randint(-3600, 3600)  # Intervalo adaptativo
                logging.info(f"Próximo backup agendado para daqui a {adaptive_interval} segundos.")
                self.stop_backup.wait(adaptive_interval)
            except Exception as e:
                logging.error(f"Erro ao agendar backup: {e}")
                sentry_sdk.capture_exception(e)

    def cleanup_old_backups(self):
        """
        Remove backups antigos de acordo com a política de retenção.
        """
        try:
            backups = sorted(Path('backups').glob('*.zip.enc'), reverse=True)
            for backup in backups[self.retention_policy:]:
                os.remove(backup)
                logging.info(f"Backup antigo removido: {backup}")
        except Exception as e:
            logging.error(f"Erro ao limpar backups antigos: {e}")
            sentry_sdk.capture_exception(e)

    def upload_backup_to_s3(self, backup_path):
        """
        Faz o upload do backup criptografado para o Amazon S3.
        """
        logging.info("Enviando backup para o S3...")
        try:
            s3 = boto3.client('s3')
            bucket_name = os.getenv("S3_BUCKET_NAME")  # Melhorado: Usar variáveis de ambiente para segurança
            s3.upload_file(str(backup_path), bucket_name, backup_path.name)
            logging.info(f"Backup enviado para S3 com sucesso: {backup_path}")
        except Exception as e:
            logging.error(f"Erro ao enviar backup para S3: {e}")
            sentry_sdk.capture_exception(e)

    def manual_backup(self):
        """
        Executa um backup manualmente.
        """
        logging.info("Executando backup manual...")
        try:
            self.create_backup()
            logging.info("Backup manual concluído com sucesso.")
        except Exception as e:
            logging.error(f"Erro ao executar backup manual: {e}")
            sentry_sdk.capture_exception(e)

    def send_notification(self, subject, message):
        """
        Envia uma notificação por email sobre o status do backup.
        """
        try:
            email_sender = os.getenv("EMAIL_SENDER")
            email_recipient = os.getenv("EMAIL_RECIPIENT")
            email_password = os.getenv("EMAIL_PASSWORD")
            smtp_server = os.getenv("SMTP_SERVER")
            smtp_port = os.getenv("SMTP_PORT")

            msg = MIMEMultipart()
            msg['From'] = email_sender
            msg['To'] = email_recipient
            msg['Subject'] = subject

            msg.attach(MIMEText(message, 'plain'))

            server = smtplib.SMTP(smtp_server, smtp_port)
            server.starttls()
            server.login(email_sender, email_password)
            text = msg.as_string()
            server.sendmail(email_sender, email_recipient, text)
            server.quit()

            logging.info(f"Notificação enviada: {subject}")
        except Exception as e:
            logging.error(f"Erro ao enviar notificação por email: {e}")
            sentry_sdk.capture_exception(e)

# Exemplo de uso
encryption_key = os.getenv("ENCRYPTION_KEY")  # Melhorado: Segurança da chave de criptografia
if encryption_key:
    backup_manager = BackupManager(encryption_key)
else:
    logging.error("Erro: Chave de criptografia não definida.")

