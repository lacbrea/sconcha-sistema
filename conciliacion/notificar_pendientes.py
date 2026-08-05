#!/usr/bin/env python3
"""Notifica pendientes.json (salida de build_conciliacion.py) a grupos de Telegram por tema.

No usa dependencias externas (solo urllib, de la libreria estandar) para poder correr en
cualquier maquina sin instalar nada.

Config (--config config.json), NUNCA con tokens reales en el repo/carpeta del skill:
  {
    "bot_token": "123456:ABC-...",
    "grupos": {
      "compras": -1001234567890,
      "pagos": -1009876543210,
      "conciliacion": -1005555555555,
      "ventas": -1004444444444
    }
  }
Temas sin grupo configurado, o sin items en pendientes.json, se omiten (no se envia nada).

Uso:
  python3 notificar_pendientes.py --pendientes pendientes.json --config config.json [--dry-run]

--dry-run: imprime en pantalla el mensaje que se enviaria a cada grupo, sin llamar a la API
de Telegram. Usar siempre --dry-run primero para revisar el contenido.
"""
import argparse
import json
import urllib.request
import urllib.parse
import urllib.error

TEMAS = ['compras', 'pagos', 'conciliacion', 'ventas']
TITULOS = {
    'compras': 'CARGOS SIN COMPROBANTE',
    'pagos': 'PAGOS / FACTURAS PENDIENTES',
    'conciliacion': 'CONCILIACION PENDIENTE (constancias / caja chica)',
    'ventas': 'VENTAS - DIFERENCIAS DE CUADRE',
}

TELEGRAM_API = 'https://api.telegram.org/bot{token}/sendMessage'


def fmt_monto(v):
    if v is None:
        return ''
    try:
        return f'S/ {abs(float(v)):,.2f}'
    except (TypeError, ValueError):
        return str(v)


def build_mensaje(tema, items):
    titulo = TITULOS.get(tema, tema.upper())
    lineas = [f'*{titulo}*', f'{len(items)} pendiente(s):', '']
    for it in items[:40]:  # limite razonable por mensaje; Telegram corta ~4096 chars igual
        fecha = it.get('fecha') or 's/f'
        monto = fmt_monto(it.get('monto'))
        desc = it.get('descripcion', '')
        motivo = it.get('motivo', '')
        linea = f'- {fecha}'
        if monto:
            linea += f' | {monto}'
        linea += f' | {desc}'
        if motivo:
            linea += f' [{motivo}]'
        lineas.append(linea)
    if len(items) > 40:
        lineas.append(f'... y {len(items) - 40} mas (ver el Excel de conciliacion).')
    return '\n'.join(lineas)


def enviar_telegram(bot_token, chat_id, mensaje, dry_run):
    if dry_run:
        print(f'--- [DRY-RUN] enviaria a chat_id={chat_id} ---')
        print(mensaje)
        print('--- fin mensaje ---\n')
        return True
    url = TELEGRAM_API.format(token=bot_token)
    data = urllib.parse.urlencode({
        'chat_id': chat_id,
        'text': mensaje,
        'parse_mode': 'Markdown',
    }).encode('utf-8')
    try:
        with urllib.request.urlopen(url, data=data, timeout=20) as resp:
            body = json.loads(resp.read().decode('utf-8'))
            if not body.get('ok'):
                print(f'ERROR Telegram (chat_id={chat_id}): {body}')
                return False
            return True
    except urllib.error.URLError as e:
        print(f'ERROR de red enviando a chat_id={chat_id}: {e}')
        return False


def main():
    ap = argparse.ArgumentParser(description='Notifica pendientes.json a Telegram por tema')
    ap.add_argument('--pendientes', required=True, help='ruta a pendientes.json')
    ap.add_argument('--config', required=True, help='ruta a config.json con bot_token y grupos')
    ap.add_argument('--dry-run', action='store_true', help='no envia nada, solo imprime')
    args = ap.parse_args()

    with open(args.pendientes, encoding='utf-8') as f:
        pendientes = json.load(f)
    with open(args.config, encoding='utf-8') as f:
        config = json.load(f)

    bot_token = config.get('bot_token', '')
    grupos = config.get('grupos', {})

    if not args.dry_run and not bot_token:
        print('ERROR: falta bot_token en el config (usa --dry-run para probar sin token).')
        return 1

    enviados = 0; omitidos = 0
    for tema in TEMAS:
        items = pendientes.get(tema, [])
        chat_id = grupos.get(tema)
        if not items:
            print(f'[{tema}] sin pendientes, se omite.')
            omitidos += 1
            continue
        if not chat_id:
            print(f'[{tema}] {len(items)} pendiente(s) pero no hay grupo configurado, se omite.')
            omitidos += 1
            continue
        mensaje = build_mensaje(tema, items)
        ok = enviar_telegram(bot_token, chat_id, mensaje, args.dry_run)
        if ok:
            enviados += 1

    print(f'\nResumen: {enviados} tema(s) notificado(s), {omitidos} omitido(s).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
