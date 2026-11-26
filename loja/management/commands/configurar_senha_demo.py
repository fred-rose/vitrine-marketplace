import getpass
import os
import warnings

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from .seed_demo import CONTAS_DEMO

SENHAS_DEMO_ENV = {
    "demo_comprador": "DEMO_BUYER_PASSWORD",
    "demo_vendedor_1": "DEMO_SELLER_1_PASSWORD",
    "demo_vendedor_2": "DEMO_SELLER_2_PASSWORD",
}


class Command(BaseCommand):
    help = "Define senhas de contas demonstrativas pelo terminal ou pelo ambiente."

    def add_arguments(self, parser):
        parser.add_argument("conta", nargs="?", choices=CONTAS_DEMO)
        parser.add_argument("--from-env", action="store_true", help="Define apenas senhas ainda desativadas.")

    def handle(self, *args, conta, from_env, **options):
        if from_env:
            if conta is not None:
                raise CommandError("Use --from-env sem informar uma conta.")
            self.configurar_do_ambiente()
            return
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

    def configurar_do_ambiente(self):
        configuradas = 0
        usuarios = get_user_model().objects
        with transaction.atomic():
            for conta, variavel in SENHAS_DEMO_ENV.items():
                senha = os.environ.get(variavel)
                if not senha:
                    continue
                try:
                    usuario = usuarios.select_for_update().get(username=conta)
                except usuarios.model.DoesNotExist:
                    raise CommandError(f"Conta {conta} ausente. Execute seed_demo primeiro.") from None
                if usuario.has_usable_password():
                    continue
                try:
                    validate_password(senha, usuario)
                except ValidationError:
                    raise CommandError(f"A senha em {variavel} não passou pela validação do Django.") from None
                anterior = usuario.password
                usuario.set_password(senha)
                configuradas += usuarios.filter(pk=usuario.pk, password=anterior).update(password=usuario.password)
        if configuradas:
            self.stdout.write(self.style.SUCCESS(f"Senhas demonstrativas configuradas: {configuradas}."))
