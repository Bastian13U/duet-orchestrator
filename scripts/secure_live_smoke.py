"""Reuse local keys. Missing keys entered here stay only in process memory."""
import getpass
import json
import os
from pathlib import Path
import sys
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    if not sys.stdin.isatty():
        print('Run this script interactively in your own terminal; never paste keys into chat.')
        return 2
    print('Prueba real OpenAI + Claude. Presupuesto acumulado: US$1.')
    print('Se reutilizan las claves configuradas; las que introduzcas aqui no se guardan.')
    supplied = []
    try:
        directory = ROOT.parent / 'live-validation'
        if directory.exists():
            print('La salida ya existe. Revisala antes de repetir; el presupuesto es compartido.')
            return 2
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            from duet.credentials import get_key, validate_key
            for key in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY'):
                if not get_key(key):
                    value = getpass.getpass(key + ' (entrada oculta): ')
                    validate_key(value)
                    os.environ[key] = value
                    supplied.append(key)
                    value = None
        from duet.__main__ import main as cli
        return cli(['live-smoke', '--directory', str(directory),
                    '--config', str(ROOT / 'examples/live-smoke.json'),
                    '--state-dir', str(ROOT.parent / '.duet-live-smoke-state')])
    except (KeyboardInterrupt, EOFError):
        print('\nPrueba interrumpida; conserva el estado antes de volver a intentarla.')
        return 2
    except Exception:
        print('No se pudo completar la prueba segura. Consulta duet doctor; no se mostraron claves.')
        return 1
    finally:
        for key in supplied:
            os.environ.pop(key, None)


if __name__ == '__main__':
    raise SystemExit(main())
