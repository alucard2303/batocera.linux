from __future__ import annotations

import json
import struct
import time
from pathlib import Path

from batocera_common import timer as timer_module
from batocera_common.timer import (
    TimerConf,
    TimerSession,
    decode_input_event,
    find_coin_device,
    read_last_exit,
    read_timeleft,
    write_timeleft,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_session(tmp_path: Path, start_remaining: int, *, warn_seconds: int = 30, clock: FakeClock | None = None, **kwargs):
    return TimerSession(
        start_remaining,
        warn_seconds=warn_seconds,
        timeleft_path=tmp_path / 'timer' / 'timeleft',
        session_file=tmp_path / 'run' / 'session',
        last_exit_file=tmp_path / 'run' / 'last_exit',
        clock=clock or FakeClock(),
        persist_interval=0,
        **kwargs,
    )


class TestTimeleftFile:
    def test_read_missing_returns_zero(self, tmp_path: Path) -> None:
        assert read_timeleft(tmp_path / 'timer' / 'timeleft') == 0

    def test_write_and_read_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / 'timer' / 'timeleft'
        write_timeleft(90, path)
        assert read_timeleft(path) == 90

    def test_write_clamps_negative(self, tmp_path: Path) -> None:
        path = tmp_path / 'timer' / 'timeleft'
        write_timeleft(-5, path)
        assert read_timeleft(path) == 0

    def test_read_ignores_garbage(self, tmp_path: Path) -> None:
        path = tmp_path / 'timer' / 'timeleft'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('not-a-number\n')
        assert read_timeleft(path) == 0


class TestLastExit:
    def test_default_is_empty(self, tmp_path: Path) -> None:
        assert read_last_exit(tmp_path / 'run' / 'last_exit') == ''

    def test_mark_and_read(self, tmp_path: Path) -> None:
        path = tmp_path / 'run' / 'last_exit'
        timer_module.mark_timeout(path)
        assert read_last_exit(path) == 'timeout'

    def test_clear_removes_marker(self, tmp_path: Path) -> None:
        path = tmp_path / 'run' / 'last_exit'
        timer_module.mark_timeout(path)
        timer_module.clear_last_exit(path)
        assert read_last_exit(path) == ''


class TestTimerConf:
    def test_disabled_by_default(self, tmp_path: Path) -> None:
        conf = tmp_path / 'batocera.conf'
        conf.write_text('timer.enabled=0\n', encoding='latin1')
        timer = TimerConf.load(conf)
        assert timer.enabled is False
        assert timer.warn_seconds == 30

    def test_enabled_with_custom_warn(self, tmp_path: Path) -> None:
        conf = tmp_path / 'batocera.conf'
        conf.write_text('timer.enabled=1\ntimer.warn=15\n', encoding='latin1')
        timer = TimerConf.load(conf)
        assert timer.enabled is True
        assert timer.warn_seconds == 15

    def test_invalid_warn_falls_back(self, tmp_path: Path) -> None:
        conf = tmp_path / 'batocera.conf'
        conf.write_text('timer.warn=abc\n', encoding='latin1')
        timer = TimerConf.load(conf)
        assert timer.warn_seconds == 30

    def test_defaults(self, tmp_path: Path) -> None:
        conf = tmp_path / 'batocera.conf'
        conf.write_text('', encoding='latin1')
        timer = TimerConf.load(conf)
        assert timer.add_minutes == 5
        assert timer.coin_device == ''

    def test_custom_minutes_and_device(self, tmp_path: Path) -> None:
        conf = tmp_path / 'batocera.conf'
        conf.write_text('timer.add.minutes=10\ntimer.coin.device=Coin Door\n', encoding='latin1')
        timer = TimerConf.load(conf)
        assert timer.add_minutes == 10
        assert timer.coin_device == 'Coin Door'


class TestTimerSession:
    def test_remaining_deducts_elapsed(self, tmp_path: Path) -> None:
        clock = FakeClock()
        session = make_session(tmp_path, 90, clock=clock, on_expire=lambda: None)
        session.start()
        try:
            clock.advance(10.4)
            assert session.remaining() == 80
        finally:
            session.stop()

    def test_remaining_never_negative(self, tmp_path: Path) -> None:
        clock = FakeClock()
        session = make_session(tmp_path, 5, clock=clock, on_expire=lambda: None)
        session.start()
        try:
            clock.advance(60)
            assert session.remaining() == 0
        finally:
            session.stop()

    def test_stop_persists_and_removes_session_file(self, tmp_path: Path) -> None:
        clock = FakeClock()
        session = make_session(tmp_path, 90, clock=clock, on_expire=lambda: None)
        timeleft = session.timeleft_path
        session.start()
        clock.advance(10)
        assert session.session_file.exists()
        session.stop()

        assert not session.session_file.exists()
        assert read_timeleft(timeleft) == 80

    def test_expiry_zeroes_state_marks_timeout_and_calls_expire(self, tmp_path: Path) -> None:
        clock = FakeClock()
        expired: list[str] = []

        session = make_session(
            tmp_path,
            10,
            warn_seconds=5,
            clock=clock,
            on_expire=lambda: expired.append('fired'),
        )
        session.start()
        try:
            clock.advance(10.5)
            time.sleep(0.3)
            assert read_timeleft(session.timeleft_path) == 0
            assert read_last_exit(session.last_exit_file) == 'timeout'
            assert expired == []

            clock.advance(5.5)
            time.sleep(0.3)
            assert expired == ['fired']
        finally:
            session.stop()

    def test_expiry_before_warn_keeps_game_running(self, tmp_path: Path) -> None:
        clock = FakeClock()
        expired: list[str] = []

        session = make_session(tmp_path, 10, warn_seconds=5, clock=clock, on_expire=lambda: expired.append('fired'))
        session.start()
        try:
            clock.advance(10.5)
            time.sleep(0.3)
            assert expired == []
        finally:
            session.stop()

    def test_start_writes_session_file(self, tmp_path: Path) -> None:
        clock = FakeClock()
        session = make_session(tmp_path, 60, clock=clock, on_expire=lambda: None)
        session.start()
        try:
            state = json.loads(session.session_file.read_text())
            assert state['start_remaining'] == 60
            assert state['started_at'] == 1000.0
            assert 'pid' in state
        finally:
            session.stop()

    def test_start_clears_timeout_marker(self, tmp_path: Path) -> None:
        clock = FakeClock()
        timer_module.mark_timeout(tmp_path / 'run' / 'last_exit')
        session = make_session(tmp_path, 60, clock=clock, on_expire=lambda: None)
        session.start()
        assert read_last_exit(session.last_exit_file) == ''
        session.stop()

    def test_coin_during_play_extends_remaining(self, tmp_path: Path) -> None:
        clock = FakeClock()
        session = make_session(tmp_path, 60, clock=clock, on_expire=lambda: None)
        session.start()
        try:
            clock.advance(20)
            session.add_seconds(300)
            assert session.remaining() == 340
            assert read_timeleft(session.timeleft_path) == 340
        finally:
            session.stop()

    def test_coin_after_expiry_revives_session(self, tmp_path: Path) -> None:
        clock = FakeClock()
        expired: list[str] = []

        session = make_session(tmp_path, 10, warn_seconds=30, clock=clock, on_expire=lambda: expired.append('fired'))
        session.start()
        try:
            clock.advance(11)
            time.sleep(0.3)
            assert session.remaining() == 0
            assert read_last_exit(session.last_exit_file) == 'timeout'

            session.add_seconds(300)
            assert session.remaining() == 300
            assert read_last_exit(session.last_exit_file) == ''
            assert expired == []

            clock.advance(10)
            time.sleep(0.3)
            assert session.remaining() == 290
            assert expired == []
        finally:
            session.stop()

    def test_stop_after_expiry_persists_zero(self, tmp_path: Path) -> None:
        clock = FakeClock()
        session = make_session(tmp_path, 10, clock=clock, on_expire=lambda: None)
        session.start()
        clock.advance(60)
        session.stop()
        assert read_timeleft(session.timeleft_path) == 0


class TestCoinDevice:
    def test_decode_input_event_key_press(self) -> None:
        data = struct.pack('llHHi', 0, 0, 1, 42, 1)  # EV_KEY, code 42, value 1
        assert decode_input_event(data) == (1, 42, 1)

    def test_decode_input_event_release(self) -> None:
        data = struct.pack('llHHi', 0, 0, 1, 42, 0)
        assert decode_input_event(data) == (1, 42, 0)

    def test_decode_incomplete_data(self) -> None:
        assert decode_input_event(b'\x00' * 8) is None

    def test_find_coin_device_matches_name_fragment(self, tmp_path: Path) -> None:
        sysfs = tmp_path / 'sys'
        events = tmp_path / 'events'
        events.mkdir()
        (sysfs / 'event0' / 'device').mkdir(parents=True)
        (sysfs / 'event1' / 'device').mkdir(parents=True)
        (sysfs / 'event0' / 'device' / 'name').write_text('X Arcade Coin Door\n')
        (sysfs / 'event1' / 'device' / 'name').write_text('Sony DualShock 4\n')
        (events / 'event0').touch()
        (events / 'event1').touch()

        assert find_coin_device('coin', events, sysfs) == events / 'event0'
        assert find_coin_device('nothing', events, sysfs) is None
        assert find_coin_device('', events, sysfs) is None
