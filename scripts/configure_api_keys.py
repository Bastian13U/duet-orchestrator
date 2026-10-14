"""Reuse configured keys; save new hidden input in Windows Credential Manager."""
import argparse
import getpass
from pathlib import Path
import sys
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Configurar las APIs en el gestor de credenciales de Windows')
    parser.add_argument('--replace', action='store_true', help='Reemplazar expresamente las claves guardadas')
    parser.add_argument('--provider', choices=['all', 'openai', 'anthropic'], default='all',
                        help='Configurar solo el proveedor elegido')
    parser.add_argument('--validate', action='store_true', help='Ejecutar despues la prueba real, limitada a US$1')
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        print('Abre este configurador en tu propia terminal. No envies claves por chat.')
        return 2
    from duet.credentials import status, store_key, CredentialError
    print('Las nuevas claves se guardaran en el Administrador de credenciales de Windows para tu usuario.')
    print('Se conservaran las claves ya configuradas en el entorno o en .env.local.')
    print('OpenAI: https://platform.openai.com/api-keys')
    print('Claude: https://platform.claude.com/settings/keys')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            for provider, name, label in (('openai', 'OPENAI_API_KEY', 'OpenAI'),
                                          ('anthropic', 'ANTHROPIC_API_KEY', 'Claude')):
                if args.provider not in ('all', provider):
                    continue
                current = status(name)
                if current['source'] == 'unavailable':
                    raise CredentialError('Credential source is unavailable')
                if current['present']:
                    if not args.replace:
                        print(label + ': clave configurada; se conserva.')
                        continue
                    if current['source'] != 'windows-credential-manager':
                        print(label + ': la clave procede del entorno o .env.local; reemplazala en su origen.')
                        return 2
                value = getpass.getpass(label + ' - pega la clave aqui (entrada oculta): ')
                store_key(name, value, replace=args.replace)
                value = None
                print(label + ': clave guardada. Su validez aun no se ha comprobado.')
        if args.validate:
            from secure_live_smoke import main as validate
            return validate()
        print('Configuracion terminada. Puedes ejecutar scripts/secure_live_smoke.py para validarla.')
        return 0
    except (CredentialError, getpass.GetPassWarning):
        print('No se pudo completar la configuracion segura. No se mostraron claves.')
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\nConfiguracion interrumpida. Las claves ya guardadas se conservan.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
