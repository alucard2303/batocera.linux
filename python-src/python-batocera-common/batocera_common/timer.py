from __future__ import annotations

import json
import logging
import os
import select
import signal
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final

from .key_value_config import KeyValueConfig
from .paths import BATOCERA_CONF, CONFIGS

_logger: Final = logging.getLogger(__name__)

# State file shared with EmulationStation (C++): plain text, seconds on a single line.
TIMELEFT_PATH: Final = CONFIGS / 'timer' / 'timeleft'

# Runtime state, stored on tmpfs so it never survives a reboot.
RUN_DIR: Final = Path('/run/batocera-timer')
SESSION_FILE: Final = RUN_DIR / 'session'
LAST_EXIT_FILE: Final = RUN_DIR / 'last_exit'

_WATCH_GRANULARITY: Final = 0.05
_DEFAULT_WARN_SECONDS: Final = 30
_DEFAULT_ADD_MINUTES: Final = 5
_DEFAULT_PERSIST_INTERVAL: Final = 30.0

_INPUT_DIR: Final = Path('/dev/input')
_SYSFS_INPUT_DIR: Final = Path('/sys/class/input')
_EVENT_STRUCT: Final = struct.Struct('llHHi')  # struct input_event (linux)
_EV_KEY: Final = 0x01


def _atomic_write(path: Path, content: str, /) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'{path.name}.tmp')
    temporary.write_text(content)
    temporary.replace(path)


def read_timeleft(path: Path = TIMELEFT_PATH, /) -> int:
    """Seconds of play time remaining; 0 when missing or unreadable."""
    try:
        return max(0, int(path.read_text().strip()))
    except (OSError, ValueError):
        return 0


def write_timeleft(seconds: int, path: Path = TIMELEFT_PATH, /) -> None:
    _atomic_write(path, f'{max(0, seconds)}\n')


def read_last_exit(path: Path = LAST_EXIT_FILE, /) -> str:
    """Last exit reason written by the launcher ('' | 'timeout')."""
    try:
        return path.read_text().strip()
    except OSError:
        return ''


def mark_timeout(path: Path = LAST_EXIT_FILE, /) -> None:
    _atomic_write(path, 'timeout\n')


def clear_last_exit(path: Path = LAST_EXIT_FILE, /) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _default_on_expire() -> None:
    # SIGINT is handled by both launch paths and turns into "kill the emulator".
    os.kill(os.getpid(), signal.SIGINT)


@dataclass(slots=True)
class TimerConf:
    enabled: bool = False
    add_minutes: int = _DEFAULT_ADD_MINUTES
    warn_seconds: int = _DEFAULT_WARN_SECONDS
    coin_device: str = ''

    @classmethod
    def load(cls, config: KeyValueConfig | Path | None = None, /) -> TimerConf:
        kv = config if isinstance(config, KeyValueConfig) else KeyValueConfig(config or BATOCERA_CONF)

        enabled = kv.get('timer.enabled', '0') == '1'

        add_minutes = _DEFAULT_ADD_MINUTES
        warn_seconds = _DEFAULT_WARN_SECONDS
        try:
            add_minutes = int(kv.get('timer.add.minutes', _DEFAULT_ADD_MINUTES))
        except ValueError:
            _logger.warning('invalid timer.add.minutes value in %s, using default', kv.path)
        try:
            warn_seconds = int(kv.get('timer.warn', _DEFAULT_WARN_SECONDS))
        except ValueError:
            _logger.warning('invalid timer.warn value in %s, using default', kv.path)

        return cls(
            enabled=enabled,
            add_minutes=max(1, add_minutes),
            warn_seconds=max(0, warn_seconds),
            coin_device=kv.get('timer.coin.device', '') or '',
        )


class TimerSession:
    """Account play time while the launcher runs and force-quit when it runs out.

    The session starts with ``start_remaining`` seconds. Coins inserted while the
    game runs (``add_seconds``) extend the session live, including reviving it when
    added during the closing grace period. When time runs out, the state file is set
    to 0, the timeout is marked and, after ``warn_seconds`` of grace, ``on_expire``
    is invoked (by default SIGINT to the current process, which both launcher paths
    translate into killing the emulator).
    """

    def __init__(
        self,
        start_remaining: int,
        /,
        *,
        warn_seconds: int = _DEFAULT_WARN_SECONDS,
        timeleft_path: Path = TIMELEFT_PATH,
        session_file: Path = SESSION_FILE,
        last_exit_file: Path = LAST_EXIT_FILE,
        persist_interval: float = _DEFAULT_PERSIST_INTERVAL,
        clock: Callable[[], float] = time.time,
        on_expire: Callable[[], None] = _default_on_expire,
    ) -> None:
        self.start_remaining = max(0, start_remaining)
        self.warn_seconds = max(0, warn_seconds)
        self.timeleft_path = timeleft_path
        self.session_file = session_file
        self.last_exit_file = last_exit_file
        self.persist_interval = persist_interval
        self._clock = clock
        self._on_expire = on_expire

        self._started_at: float | None = None
        self._expired = False
        self._credits = 0
        self._expire_at: float | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return

        with self._lock:
            self._started = True
            self._started_at = self._clock()
            self._expired = False
            self._credits = 0
            self._expire_at = None
            self._stop.clear()
            clear_last_exit(self.last_exit_file)

            _atomic_write(
                self.session_file,
                json.dumps(
                    {'start_remaining': self.start_remaining, 'started_at': self._started_at, 'pid': os.getpid()},
                    sort_keys=True,
                )
                + '\n',
            )

        self._thread = threading.Thread(target=self._watch, daemon=True, name='batocera-timer')
        self._thread.start()

    def _unclamped_remaining(self) -> int:
        assert self._started_at is not None
        elapsed = max(0, int(self._clock() - self._started_at))
        return self.start_remaining + self._credits - elapsed

    def remaining(self) -> int:
        if self._started_at is None or self._expired:
            return 0 if self._expired else self.start_remaining

        return max(0, self._unclamped_remaining())

    def add_seconds(self, seconds: int, /) -> None:
        """Add play time to the running session (a coin was inserted)."""
        if seconds <= 0 or not self._started:
            return

        with self._lock:
            if self._expired or self._unclamped_remaining() <= 0:
                # Inserted after the time ran out (grace period): restart the session fresh.
                self._started_at = self._clock()
                self.start_remaining = seconds
                self._credits = 0
                self._expired = False
                self._expire_at = None
                clear_last_exit(self.last_exit_file)
            else:
                self._credits += seconds

            write_timeleft(self.remaining(), self.timeleft_path)
            _logger.info('Coin inserted: +%s seconds of play time', seconds)

    def stop(self) -> None:
        if not self._started:
            return

        with self._lock:
            self._started = False
            self._stop.set()

        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

        write_timeleft(self.remaining(), self.timeleft_path)

        try:
            self.session_file.unlink()
        except OSError:
            pass

    def _watch(self) -> None:
        next_persist = self._clock() + self.persist_interval if self.persist_interval > 0 else None

        while not self._stop.is_set():
            now = self._clock()
            close_emulator = False

            with self._lock:
                if self._expired:
                    if self._expire_at is not None and now >= self._expire_at:
                        close_emulator = True
                        _logger.info('Play time exhausted: closing the emulator')
                        self._stop.set()
                else:
                    if self.remaining() == 0:
                        self._expired = True
                        self._expire_at = now + self.warn_seconds
                        write_timeleft(0, self.timeleft_path)
                        mark_timeout(self.last_exit_file)
                        _logger.info('Play time exhausted: emulator will be closed in %s seconds', self.warn_seconds)
                    elif next_persist is not None and now >= next_persist:
                        write_timeleft(self.remaining(), self.timeleft_path)
                        next_persist = now + self.persist_interval

            if close_emulator:
                self._on_expire()
                return

            self._stop.wait(_WATCH_GRANULARITY)


def decode_input_event(data: bytes, /) -> tuple[int, int, int] | None:
    """Decode one linux input_event frame: (type, code, value); None if incomplete."""
    if len(data) < _EVENT_STRUCT.size:
        return None

    _type, _code, value = _EVENT_STRUCT.unpack(data[:_EVENT_STRUCT.size])[2:]
    return _type, _code, value


def find_coin_device(
    fragment: str,
    /,
    input_dir: Path = _INPUT_DIR,
    sysfs_dir: Path = _SYSFS_INPUT_DIR,
) -> Path | None:
    """First /dev/input/eventX whose sysfs device name contains the fragment."""
    fragment_lower = fragment.lower()
    if not fragment_lower:
        return None

    try:
        candidates = sorted(input_dir.glob('event*'), key=lambda p: p.name)
    except OSError:
        return None

    for event_path in candidates:
        name_file = sysfs_dir / event_path.name / 'device' / 'name'
        try:
            name = name_file.read_text().strip().lower()
        except OSError:
            continue

        if fragment_lower in name:
            return event_path

    return None


class CoinDeviceWatcher:
    """Watch a dedicated input device (the coin acceptor) and fire on each press.

    The device is read in parallel with the emulator; emulators do not grab evdev
    keyboards/buttons exclusively, so a second reader observes the coin events.
    """

    def __init__(
        self,
        device: Path,
        on_coin: Callable[[], None],
        /,
        *,
        clock: Callable[[], float] = time.time,
        debounce: float = 0.2,
    ) -> None:
        self.device = device
        self._on_coin = on_coin
        self._clock = clock
        self._debounce = debounce
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._last_coin_at = 0.0

    def start(self) -> None:
        if self._thread is not None:
            return

        self._thread = threading.Thread(target=self._run, daemon=True, name='batocera-coin')
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        try:
            fd = os.open(self.device, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            _logger.warning('Cannot open coin device %s: %s', self.device, e)
            return

        self._fd = fd
        buffer = bytearray()
        try:
            while not self._stop.is_set():
                readable, _, _ = select.select([fd], [], [], _WATCH_GRANULARITY)
                if not readable:
                    continue

                try:
                    chunk = os.read(fd, 4096)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError as e:
                    _logger.warning('Coin device %s read error: %s', self.device, e)
                    break

                if not chunk:
                    break

                buffer.extend(chunk)
                while len(buffer) >= _EVENT_STRUCT.size:
                    event = decode_input_event(bytes(buffer))
                    if event is None:
                        break

                    del buffer[:_EVENT_STRUCT.size]
                    self._handle_event(*event)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            self._fd = None

    def _handle_event(self, event_type: int, code: int, value: int, /) -> None:
        if event_type != _EV_KEY or value != 1:
            return

        now = self._clock()
        if now - self._last_coin_at < self._debounce:
            return

        self._last_coin_at = now
        _logger.info('Coin event on %s (code %s)', self.device, code)
        try:
            self._on_coin()
        except Exception:
            _logger.exception('coin callback failed')
