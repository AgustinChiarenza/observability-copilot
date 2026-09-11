"""CLI: `python -m copilot <comando>`.

`preflight` es el comando importante y por eso existe desde F0: es lo que se
corre en la instalación, en la máquina del cliente, **antes de irse**. Toca cada
puerto configurado y dice cuál no responde y por qué. Sin él, la forma de
descubrir que el token de Prometheus estaba mal es que el agente no conteste el
martes siguiente, con vos a 500 km.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import __version__, adapters, logging_setup
from .config import ConfigError, load
from .runtime import build


def _cmd_adapters(_args) -> int:
    for kind, nombres in adapters.catalog().items():
        print(f"{kind}:")
        for n in nombres:
            print(f"  - {n}")
    return 0


def _cmd_preflight(args) -> int:
    cfg = load(args.config)
    rt = build(cfg)
    estado = asyncio.run(rt.check())

    print(f"config: {cfg.source_path}")
    for puerto, valor in rt.describe().items():
        print(f"  {puerto:<12} {valor or '(sin configurar)'}")
    print()

    fallas = 0
    for puerto, resultado in estado.items():
        if resultado == "ok":
            marca = "OK  "
        elif resultado == "not configured":
            marca = "--  "
        else:
            marca, fallas = "FALLA", fallas + 1
        print(f"  [{marca:<5}] {puerto:<18} {resultado}")

    if fallas:
        print(f"\n{fallas} puerto(s) no responden. El agente va a arrancar igual, "
              f"pero eso que falla no va a estar disponible.", file=sys.stderr)
    else:
        print("\nTodos los puertos configurados responden.")
    return 1 if fallas else 0


def _cmd_tool(args) -> int:
    """Corre UNA tool, sin modelo de por medio.

    Sirve para dos cosas que no son tests: verificar en la instalación que el
    agente ve los datos que tiene que ver —sin gastar un token ni depender de
    que el LLM esté arriba— y depurar "¿por qué contestó eso?" mirando lo mismo
    que vio él.
    """
    import asyncio as _a

    from .agent import registry

    cfg = load(args.config)
    rt = build(cfg)
    try:
        argumentos = json.loads(args.args) if args.args else {}
    except json.JSONDecodeError as e:
        print(f"Los argumentos tienen que ser JSON: {e}", file=sys.stderr)
        return 2
    if not registry.known(args.tool):
        disponibles = ", ".join(d["function"]["name"]
                                for d in registry.definitions(rt.context()))
        print(f"No existe la tool '{args.tool}'. Disponibles: {disponibles}",
              file=sys.stderr)
        return 2

    r = _a.run(registry.execute(args.tool, argumentos, rt.context()))
    print(json.dumps(r["result"], ensure_ascii=False, indent=2, default=str))
    return 0 if r["status"] == "ok" else 1


def _cmd_ask(args) -> int:
    from . import service

    cfg = load(args.config)
    rt = build(cfg)
    r = asyncio.run(service.answer(rt, args.question, actor="cli"))
    if args.json:
        print(json.dumps({
            "text": r.text, "tokens": r.tokens, "model": r.model,
            "stopped_by": r.stopped_by,
            "steps": [{"tool": s.tool, "args": s.args, "ok": s.ok, "ms": s.ms}
                      for s in r.steps],
        }, ensure_ascii=False, indent=2))
        return 0
    for s in r.steps:
        print(f"  · {s.tool}({json.dumps(s.args, ensure_ascii=False)[:120]}) "
              f"{'ok' if s.ok else 'ERROR'} {s.ms}ms", file=sys.stderr)
    print(r.text)
    return 0


def _cmd_notify_test(args) -> int:
    """Manda un mensaje de prueba por todos los canales, salteando la política.
    Es el "¿llega?" de la instalación."""
    from .ports.notify import Message, Severity

    cfg = load(args.config)
    rt = build(cfg)
    m = Message(title="Copilot: mensaje de prueba", body=args.body,
                severity=Severity.INFO, fingerprint="notify_test")
    out = asyncio.run(rt.dispatcher.send(m, force=True))
    fallas = 0
    for d in out.deliveries:
        marca = "OK  " if d.ok else "FALLA"
        fallas += 0 if d.ok else 1
        print(f"  [{marca:<5}] {d.channel:<18} {d.detail}")
    return 1 if fallas or not out.deliveries else 0


def _cmd_detect(args) -> int:
    """Corre un detector una vez. Con --dry-run evalúa y no despacha."""
    from .detectors import cost_spike

    cfg = load(args.config)
    rt = build(cfg)
    if args.dry_run:
        if rt.cost is None:
            print("No hay CostPort configurado.", file=sys.stderr)
            return 2
        v = asyncio.run(cost_spike.evaluate(rt.cost, cfg.detectors.cost_spike))
        salida = v.as_dict()
    else:
        salida = asyncio.run(cost_spike.run_once(rt))
    print(json.dumps(salida, ensure_ascii=False, indent=2, default=str))
    return 0 if salida["outcome"] in ("spike", "clear", "no_data") else 1


def _cmd_serve(args) -> int:
    import uvicorn

    cfg = load(args.config)
    from .api import create_app

    uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port,
                log_config=None)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging_setup.setup()
    p = argparse.ArgumentParser("copilot", description=f"Copilot {__version__}")
    p.add_argument("-c", "--config", default=None, help="Ruta al copilot.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("adapters", help="Lista los adapters instalados").set_defaults(
        fn=_cmd_adapters)
    sub.add_parser("preflight", help="Chequea todos los puertos configurados").set_defaults(
        fn=_cmd_preflight)
    sub.add_parser("serve", help="Levanta la API").set_defaults(fn=_cmd_serve)

    t = sub.add_parser("tool", help="Corre una tool sola, sin modelo")
    t.add_argument("tool")
    t.add_argument("args", nargs="?", default="", help='Argumentos en JSON, ej: \'{"query":"up"}\'')
    t.set_defaults(fn=_cmd_tool)

    nt = sub.add_parser("notify-test", help="Manda un mensaje de prueba por cada canal")
    nt.add_argument("--body", default="Si estás leyendo esto, el canal quedó enchufado.")
    nt.set_defaults(fn=_cmd_notify_test)

    det = sub.add_parser("detect", help="Corre un detector una vez")
    det.add_argument("detector", choices=["cost_spike"])
    det.add_argument("--dry-run", action="store_true", help="Evalúa sin despachar")
    det.set_defaults(fn=_cmd_detect)

    ask = sub.add_parser("ask", help="Una pregunta, desde la terminal")
    ask.add_argument("question")
    ask.add_argument("--json", action="store_true")
    ask.set_defaults(fn=_cmd_ask)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (ConfigError, adapters.UnknownAdapter, ValueError) as e:
        # Un adapter que no puede armarse (opción que falta, SDK no instalado)
        # es un error de configuración: se dice en una línea, sin traceback.
        print(f"\n{e}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
