"""
monitoring.py — Operational instrumentation pentru bot-uri BP
=============================================================

Trei capabilitati, partajate intre `main.py` (single-pair) si `main_multi.py`
(multi-pair):

  1. memory_monitor()              — task async, log RSS/VMS/threads/fds/gc_obj
                                     la fiecare 5min. La spike >RSS_ALERT_MB:
                                     one-shot diagnostic dump (top 20 types via
                                     gc.get_objects + /proc/<pid>/status) +
                                     pre-OOM Telegram alert.

  2. install_signal_handlers()     — intercepteaza SIGTERM/SIGINT/SIGHUP.
                                     Loggeaza numele semnalului in
                                     SHUTDOWN_SIGNAL["name"] (citit ulterior in
                                     lifespan finally pentru Telegram shutdown
                                     notification).

  3. install_asyncio_exception_handler() — task-uri care arunca exception fara
                                     sa fie await-ed sunt loggate cu traceback.
                                     Altfel apar doar la GC ca
                                     "Task exception was never retrieved".

ENV vars relevante:
  MEM_MON_INTERVAL_SEC   — interval log RSS (default 300 = 5min).
  MEM_MON_RSS_ALERT_MB   — threshold pt diagnostic dump + pre-OOM TG alert
                           (default 320). Tunat ca sa fire INAINTE de
                           docker-compose `mem_limit` (default 384m) →
                           ~83% headroom. Daca cresti `mem_limit`, mareste
                           proportional.

DE CE pre-OOM alert e necesar in BP:
  Telegram CRASH alert din `sys.excepthook` (in main.py / main_multi.py) NU se
  invoca la SIGKILL. OOM killer trimite SIGKILL — proces moare instant, fara
  chance sa ruleze cod Python. Singura fereastra de alerta e INAINTE de kill,
  detectand RSS care creste catre limita. _memory_monitor()'s threshold trigger
  acopera exact acest gap.

INTERPRETARE EXIT CODES post-mortem (verifica cu
`docker inspect <container> --format '{{.State.ExitCode}} OOMKilled={{.State.OOMKilled}}'`):

  ExitCode  OOMKilled  Signal log               Cauza
  ────────  ─────────  ───────────────────────  ──────────────────────────────
  137       true       (none — SIGKILL)         OOM killer (sau `docker kill -9`)
  143       false      SIGTERM logged           `docker stop`, restart policy
  130       false      SIGINT logged            Ctrl-C manual
  129       false      SIGHUP logged            Parent shell hangup
  0         false      varies                   Lifespan returned graceful
  != 0      false      varies                   Uncaught exception (vezi log)

  IMPORTANT: ExitCode=0 + OOMKilled=false NU e OOM. E shutdown gratios
  (eventual cu silent exit din lifespan / try-except mut). Investigatie
  diferita.
"""
from __future__ import annotations

import asyncio
import os
import signal
import traceback
from typing import Any, Awaitable, Callable, Optional


# State partajat — citit din lifespan finally pt Telegram shutdown notification.
# Updated de signal handler.
SHUTDOWN_SIGNAL: dict[str, Optional[str]] = {"name": None}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


_persistent_logs_installed = False


def install_persistent_logs() -> None:
    """Log-uri PERSISTENTE fara a atinge sutele de print() din framework.

    `docker logs` sunt legate de INSTANTA containerului → dispar definitiv la
    re-pull image / recreare in Portainer. Redirectam sys.stdout+stderr printr-un
    "tee" care scrie SIMULTAN la stdout-ul original (pastrat pt `docker logs` live)
    SI intr-un fisier rotativ pe DATA_DIR (=/data, montat din BOT_DATA_DIR pe host
    → supravietuieste recrearii). Accesibil oricand:
        docker exec <bot> cat /data/app.log     (sau /data/app.log.1, .2 ...)
        /srv/bots/<BOT_NAME>/data/app.log        (direct pe host)

    Non-invaziv: print() ramane peste tot, doar stream-ul de dedesubt e teed.
    Cheama PRIMUL in main.py (inainte de orice print) ca sa prinzi si bannerul.
    No-op daca DATA_DIR nu e setat (persistenta OFF — modelul default al BP) sau
    daca a fost deja instalat (idempotent). Rotatie pe dimensiune printr-un sink
    propriu (LOG_MAX_BYTES/LOG_BACKUP_COUNT, ca json-file driver-ul Docker) — NU
    logging.handlers, ca sa nu fie inchis de dictConfig-ul intern al uvicorn.
    (port BP 134babf/607bd74/4bee601/d7059a2)"""
    global _persistent_logs_installed
    if _persistent_logs_installed:
        return
    data_dir = os.getenv("DATA_DIR", "")
    if not data_dir:
        return
    import sys
    import threading
    try:
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, "app.log")
        max_bytes = _env_int("LOG_MAX_BYTES", 10 * 1024 * 1024)   # 10MB
        backup_count = _env_int("LOG_BACKUP_COUNT", 5)
        lock = threading.Lock()   # PARTAJAT intre tee-ul de stdout si cel de stderr

        # Sink PROPRIU (fisier plain), NU logging.handlers.RotatingFileHandler:
        # ORICE logging.Handler se auto-inregistreaza in logging._handlerList la
        # __init__, iar uvicorn.run() cheama intern logging.config.dictConfig(
        # uvicorn LOGGING_CONFIG) care face shutdown() la TOATE handler-ele din acel
        # registru — inclusiv pe al nostru, desi nu e atasat la NICIUN logger. Efect:
        # dupa boot handler.stream devine None si tot ce scriem se pierde silentios
        # (AttributeError inghitit), deci fisierul capta DOAR fereastra minuscula
        # install_persistent_logs() → uvicorn.run(). Un fisier deschis direct e
        # complet in afara subsistemului logging → imun la dictConfig/shutdown.
        class _RotatingSink:
            def __init__(self) -> None:
                self.stream = open(path, "a", encoding="utf-8")

            def write(self, s: str) -> None:
                self.stream.write(s)
                # flush dupa FIECARE write: fisierul e block-buffered (~8KB) de OS,
                # spre deosebire de stdout (unbuffered via PYTHONUNBUFFERED). Fara
                # flush, ultimele linii raman in buffer si se PIERD la SIGKILL/OOM.
                self.stream.flush()
                # Rotatie pe dimensiune (mirror RotatingFileHandler): cap disc =
                # max_bytes x (backup_count + 1).
                if max_bytes > 0 and self.stream.tell() >= max_bytes:
                    self._rollover()

            def _rollover(self) -> None:
                self.stream.close()
                if backup_count > 0:
                    for i in range(backup_count - 1, 0, -1):
                        src, dst = f"{path}.{i}", f"{path}.{i + 1}"
                        if os.path.exists(src):
                            if os.path.exists(dst):
                                os.remove(dst)
                            os.rename(src, dst)
                    dst1 = f"{path}.1"
                    if os.path.exists(dst1):
                        os.remove(dst1)
                    os.rename(path, dst1)
                    self.stream = open(path, "a", encoding="utf-8")
                else:
                    # fara backups: taie la max_bytes (cap disc = max_bytes)
                    self.stream = open(path, "w", encoding="utf-8")

            def flush(self) -> None:
                try:
                    self.stream.flush()
                except Exception:
                    pass

        sink = _RotatingSink()

        class _Tee:
            def __init__(self, original: Any) -> None:
                self._orig = original

            def write(self, s: str) -> int:
                try:
                    self._orig.write(s)
                except Exception:
                    pass
                with lock:
                    try:
                        sink.write(s)
                    except Exception:
                        pass
                return len(s)

            def flush(self) -> None:
                try:
                    self._orig.flush()
                except Exception:
                    pass
                with lock:
                    sink.flush()

            def isatty(self) -> bool:
                return getattr(self._orig, "isatty", lambda: False)()

            def fileno(self) -> int:
                # Fd-ul original — scrierile C/subprocess merg direct la stdout
                # real (NU sunt teed). Rar; print() trece prin write() → teed.
                return self._orig.fileno()

        sys.stdout = _Tee(sys.stdout)
        sys.stderr = _Tee(sys.stderr)
        _persistent_logs_installed = True
        print(f"  [LOGS] persistent → {path} "
              f"(maxBytes={max_bytes}, backups={backup_count})")
    except Exception as e:
        # NU pica boot-ul pt logging — scrie pe stdout-ul REAL.
        try:
            (sys.__stdout__ or sys.stdout).write(
                f"  [LOGS] persistent log setup failed: {e}\n")
        except Exception:
            pass


def install_signal_handlers() -> None:
    """
    Intercepteaza SIGTERM/SIGINT/SIGHUP. Loggeaza numele si seteaza
    SHUTDOWN_SIGNAL["name"] pt referinta ulterioara in shutdown notification.

    Limitare cunoscuta: SIGKILL (OOM, `docker kill -9`) NU poate fi
    interceptat. Detectia OOM se face via memory_monitor pre-OOM alert si
    via post-mortem `docker inspect` (OOMKilled flag).
    """
    def _on_signal(signum: int, _frame: Any) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = f"signum={signum}"
        SHUTDOWN_SIGNAL["name"] = name
        print(f"  [SIGNAL] received {name} ({signum}) — initiating shutdown",
              flush=True)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _on_signal)
        except (OSError, ValueError) as e:
            # Windows nu are SIGHUP; threads non-main nu pot inregistra handlers.
            print(f"  [SIGNAL] register {sig.name} failed: {e}")


def install_asyncio_exception_handler() -> None:
    """
    Global asyncio task exception handler. Task-uri spawned via
    `asyncio.create_task(...)` care arunca exception FARA caller care le
    await-uieste sunt logged silent (doar la GC, dupa N secunde).
    Cu handler-ul ăsta apar imediat in log cu traceback complet.
    """
    def _handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        task = context.get("task") or context.get("future")
        msg = context.get("message", "")
        print(f"  [ASYNCIO_EXC] task={task} msg={msg!r}", flush=True)
        if isinstance(exc, BaseException):
            tb = "".join(traceback.format_exception(type(exc), exc,
                                                    exc.__traceback__))
            print(f"  [ASYNCIO_EXC] traceback:\n{tb}", flush=True)

    try:
        asyncio.get_event_loop().set_exception_handler(_handler)
    except RuntimeError:
        # No running loop yet — caller should invoke after loop creation.
        # (FastAPI lifespan rulează după ce loop-ul există, deci ok.)
        pass


async def supervise(
    name:         str,
    coro_factory: Callable[[], Awaitable[None]],
    tg_alert:     Optional[Callable[[str, str], Awaitable[None]]] = None,
    max_backoff:  float = 60.0,
) -> None:
    """Supervisor restart-on-death pentru un task de fundal (WS etc.).

    Ruleaza `coro_factory()`; daca task-ul MOARE (raise) sau returneaza (o bucla
    WS infinita n-ar trebui sa returneze) → alerta Telegram cu traceback COMPLET
    + auto-restart cu backoff exponential (cap `max_backoff`). Doua beneficii:
      - prinzi cauza data viitoare (traceback in alerta, nu pierdut la GC),
      - auto-vindecare (nu mai sta ore cu WS mort).

    tg_alert: foloseste `send_warning` — NU HALT (botul se auto-vindeca prin
    restart, deci NU s-a oprit). CancelledError propaga (shutdown legit — NU
    restarta). Wrap fiecare task de fundal in lifespan:
        asyncio.create_task(supervise("bybit_ws", _bybit_ws_task,
                                      tg_alert=tg.send_warning))
    """
    import html as _html
    backoff = 1.0
    while True:
        died_tb: Optional[str] = None
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise   # shutdown legit — nu restarta
        except Exception:
            died_tb = traceback.format_exc()
        if died_tb:
            print(f"  [SUPERVISOR] {name} A MURIT — restart în {backoff:.0f}s:\n"
                  f"{died_tb}", flush=True)
            if tg_alert is not None:
                try:
                    await tg_alert(
                        f"WS task '{name}' a murit — auto-restart",
                        f"Botul se auto-vindecă (restart în ~{backoff:.0f}s).\n"
                        f"<pre>{_html.escape(died_tb[-1400:])}</pre>",
                    )
                except Exception as e:
                    print(f"  [SUPERVISOR] {name} tg alert failed: {e}", flush=True)
        else:
            print(f"  [SUPERVISOR] {name} a returnat (n-ar trebui) — "
                  f"restart în {backoff:.0f}s", flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


async def memory_monitor(
    bot_name:           str,
    tg_alert:           Optional[Callable[[str, str], Awaitable[None]]] = None,
    interval_sec:       Optional[int] = None,
    rss_alert_mb:       Optional[float] = None,
) -> None:
    """
    Background task — log periodic memory + diagnostic la spike + pre-OOM alert.

    Args:
      bot_name:     pt prefix in Telegram alert (identificare bot).
      tg_alert:     `tg.send_critical` sau None (skip Telegram alerts).
      interval_sec: override MEM_MON_INTERVAL_SEC env (default 300).
      rss_alert_mb: override MEM_MON_RSS_ALERT_MB env (default 320).

    Comportament:
      - La fiecare interval: log RSS/VMS/threads/fds/gc_obj.
      - La PRIMUL sample cu RSS > rss_alert_mb:
        - Dump top 20 obiecte Python (gc.get_objects() + Counter).
        - Dump /proc/<pid>/status (Linux only).
        - Trimite Telegram CRITICAL (pre-OOM warning).
        - Set flag local "snapshot_taken" — nu mai trigger din nou
          (one-shot per boot, fara spam).

    Distinctie diagnostic (`gc_obj` count):
      - gc_obj creste monoton + RSS creste = leak Python-level
        (referinte care nu se elibereaza — circular refs, caches uitate).
      - RSS creste dar gc_obj stabil = leak C-extension (numpy/pandas
        buffers, httpx connection pool, websockets sockets).

    Safe fail: psutil import fail / sample fail / /proc absent (Mac/Windows)
    nu opresc task-ul — logam si continuam.
    """
    interval = interval_sec if interval_sec is not None else _env_int(
        "MEM_MON_INTERVAL_SEC", 300)
    alert_mb = (rss_alert_mb if rss_alert_mb is not None
                else float(os.getenv("MEM_MON_RSS_ALERT_MB", "320")))

    try:
        import psutil
        proc = psutil.Process()
    except Exception as e:
        print(f"  [MEM_MON] psutil unavailable ({e}) — task disabled",
              flush=True)
        return

    print(f"  [MEM_MON] started: interval={interval}s  "
          f"alert_threshold={alert_mb:.0f}MB", flush=True)

    snapshot_taken = False

    while True:
        try:
            import gc
            mi = proc.memory_info()
            rss_mb = mi.rss / 1024 / 1024
            vms_mb = mi.vms / 1024 / 1024
            n_threads = proc.num_threads()
            n_fds = proc.num_fds() if hasattr(proc, "num_fds") else -1
            n_gc_obj = len(gc.get_objects())
            print(
                f"  [MEM_MON] RSS={rss_mb:.1f}MB  VMS={vms_mb:.1f}MB  "
                f"threads={n_threads}  fds={n_fds}  gc_obj={n_gc_obj}",
                flush=True,
            )

            if rss_mb > alert_mb and not snapshot_taken:
                snapshot_taken = True
                print(
                    f"  [MEM_MON] ⚠️  RSS SPIKE {rss_mb:.1f}MB > {alert_mb:.0f}MB"
                    f" — one-shot diagnostic dump",
                    flush=True,
                )
                # Top 20 types — vedem dintr-o privire daca 10000× Trade
                # sau 50000× dict ccxt.
                top20_text = "(unavailable)"
                try:
                    from collections import Counter
                    type_counts = Counter(type(o).__name__
                                          for o in gc.get_objects())
                    top20 = type_counts.most_common(20)
                    print(f"  [MEM_MON] Top 20 types by count:", flush=True)
                    for tname, cnt in top20:
                        print(f"  [MEM_MON]   {cnt:>8d}  {tname}", flush=True)
                    top20_text = "\n".join(f"{cnt:>8d}  {t}"
                                           for t, cnt in top20[:10])
                except Exception as e:
                    print(f"  [MEM_MON] type_counts failed: {e}", flush=True)

                # /proc/<pid>/status (Linux). Tacem pe Mac/Windows.
                proc_status_text = ""
                try:
                    with open(f"/proc/{os.getpid()}/status") as f:
                        proc_status_text = f.read()
                    print(f"  [MEM_MON] /proc/status:\n{proc_status_text}",
                          flush=True)
                except Exception as e:
                    print(f"  [MEM_MON] /proc/status unavailable: {e}",
                          flush=True)

                # Pre-OOM Telegram alert. CRITICAL — user-ul are timp sa
                # investigheze inainte sa fie killed (excepthook NU se
                # invoca la SIGKILL, deci asta e singura fereastra).
                if tg_alert is not None:
                    try:
                        await tg_alert(
                            f"⚠️ {bot_name} — HIGH MEMORY USAGE",
                            f"<b>RSS:</b> {rss_mb:.1f} MB "
                            f"(threshold {alert_mb:.0f} MB)\n"
                            f"<b>VMS:</b> {vms_mb:.1f} MB\n"
                            f"<b>Threads:</b> {n_threads}  "
                            f"<b>FDs:</b> {n_fds}  "
                            f"<b>gc_obj:</b> {n_gc_obj}\n"
                            f"\nApropiere de mem_limit Docker — risc OOM kill.\n"
                            f"Diagnostic top types (in loguri):\n"
                            f"<pre>{top20_text}</pre>"
                        )
                    except Exception as e:
                        print(f"  [MEM_MON] tg_alert failed: {e}",
                              flush=True)
        except Exception as e:
            print(f"  [MEM_MON] sample failed: {e}", flush=True)

        await asyncio.sleep(interval)
