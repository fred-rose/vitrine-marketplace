import getpass
import warnings

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from .seed_demo import CONTAS_DEMO


class Command(BaseCommand):
    help = "Define a senha de uma conta demonstrativa em um terminal interativo."

    def add_arguments(self, parser):
        parser.add_argument("conta", choices=CONTAS_DEMO)

    def handle(self, *args, conta, **options):
        if conta not in CONTAS_DEMO:
            raise CommandError("Informe uma conta demonstrativa.")
        usuarios = get_user_model().objects
        try:
            usuario = usuarios.get(username=conta)
        except usuarios.model.DoesNotExist as erro:
            raise CommandError("Conta demonstrativa ausente. Execute seed_demo primeiro.") from erro

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                senha = getpass.getpass("Nova senha: ")
                confirmacao = getpass.getpass("Confirme a senha: ")
        except (getpass.GetPassWarning, EOFError) as erro:
            raise CommandError("Use um terminal interativo com entrada oculta.") from erro
        if not senha or senha != confirmacao:
            raise CommandError("As senhas devem ser preenchidas e coincidir.")
        try:
            validate_password(senha, usuario)
        except ValidationError as erro:
            raise CommandError(" ".join(erro.messages)) from erro
        usuario.set_password(senha)
        usuario.save(update_fields=("password",))
        self.stdout.write(self.style.SUCCESS(f"Senha configurada para {conta}."))
