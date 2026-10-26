import json

import pytest

import varipro_bridge as vb


class FakeBus:
    """Muistissa toimiva VariPro: holding- ja input-rekisterit sanakirjoissa."""

    def __init__(self):
        self.holding = {0: vb.MODE_NORMAL, 1: 0, 2: 4, 3: 215, 4: 260, 201: 20}
        self.input = {i: 0 for i in range(3, 11)}
        self.input.update({3: 2, 6: 223, 7: 215, 102: 0, 115: 80, 116: 120})
        self.writes = []
        self.fail_writes = set()   # rekisterit, joiden kirjoitus epäonnistuu
        self.offline = False
        self.last_error = None

    def read(self, kind, start, count):
        if self.offline:
            self.last_error = 'timeout'
            return None
        regs = self.holding if kind == 'holding' else self.input
        return [regs.get(a, 0) for a in range(start, start + count)]

    def write(self, addr, value):
        if self.offline or addr in self.fail_writes:
            self.last_error = 'write failed'
            return False
        self.writes.append((addr, value))
        self.holding[addr] = value
        return True

    def close(self):
        pass


class FakeMqtt:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, retain=False, qos=0):
        self.messages.append((topic, payload))

    def last(self, topic):
        for t, p in reversed(self.messages):
            if t == topic:
                return p
        return None


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    bus, mqtt, clock = FakeBus(), FakeMqtt(), Clock()
    cfg = vb.Config(state_dir=str(tmp_path))
    bridge = vb.VariProBridge(cfg, bus=bus, mqtt_client=mqtt, clock=clock)
    return bridge, bus, mqtt, clock


def poll_all(bridge, clock):
    bridge.last_poll.clear()
    bridge.poll_due_groups()
    clock.now += 1


def cmd(bridge, name, payload):
    bridge.handle_command(name, payload)


# --- Konfiguraatio ja apufunktiot ---

def test_config_from_env(tmp_path):
    cfg = vb.Config.from_env({'VARIPRO_MODBUS_PORT': '1502', 'VARIPRO_MQTT_USER': '',
                              'VARIPRO_TOPIC_PREFIX': 'vp2', 'STATE_DIRECTORY': str(tmp_path)})
    assert cfg.modbus_port == 1502
    assert cfg.mqtt_user == ''
    assert cfg.topic_prefix == 'vp2'
    assert cfg.state_dir == str(tmp_path)


def test_to_raw10_rounds_instead_of_truncating():
    assert vb.to_raw10('21.7') == 217
    assert vb.to_raw10(22.5) == 225


# --- Pollaus ja johdetut tilat ---

def test_initial_poll_publishes_state(env):
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    assert mqtt.last('varipro/mode') == 'cool'
    assert mqtt.last('varipro/preset') == 'home'
    assert mqtt.last('varipro/temp/target') == '21.5'
    assert mqtt.last('varipro/temp/target_away') == '26.0'
    assert mqtt.last('varipro/fan') == 'auto'
    assert mqtt.last('varipro/temp/current') == '22.3'
    assert mqtt.last('varipro/action') == 'idle'
    assert mqtt.last('varipro/modbus/status') == 'online'
    assert mqtt.last('varipro/alarm/suodatin') == 'OFF'


def test_unchanged_values_not_republished(env):
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    count = len(mqtt.messages)
    poll_all(bridge, clock)
    assert len(mqtt.messages) == count


def test_away_preset_uses_away_target(env):
    bridge, bus, mqtt, clock = env
    bus.holding[0] = vb.MODE_AWAY
    poll_all(bridge, clock)
    assert mqtt.last('varipro/preset') == 'away'
    assert mqtt.last('varipro/temp/target') == '26.0'


@pytest.mark.parametrize('cooling,heating,fan,mode,expected', [
    (30, 0, 50, vb.MODE_NORMAL, 'cooling'),
    (0, 20, 50, vb.MODE_NORMAL, 'heating'),
    (0, 0, 50, vb.MODE_NORMAL, 'fan'),
    (0, 0, 0, vb.MODE_NORMAL, 'idle'),
    (30, 0, 50, vb.MODE_OFF, 'off'),
])
def test_action(env, cooling, heating, fan, mode, expected):
    bridge, bus, mqtt, clock = env
    bus.holding[0] = mode
    bus.input.update({9: cooling, 10: heating, 8: fan})
    poll_all(bridge, clock)
    assert mqtt.last('varipro/action') == expected


def test_alarm_bits(env):
    bridge, bus, mqtt, clock = env
    bus.input[102] = (1 << 0) | (1 << 15)
    poll_all(bridge, clock)
    assert mqtt.last('varipro/alarm/kondenssi') == 'ON'
    assert mqtt.last('varipro/alarm/suodatin') == 'ON'
    assert mqtt.last('varipro/alarm/puhallin1_rpm') == 'OFF'


def test_modbus_offline_after_repeated_failures(env):
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    bus.offline = True
    for _ in range(vb.MODBUS_FAIL_LIMIT - 1):
        poll_all(bridge, clock)
    assert mqtt.last('varipro/modbus/status') == 'online'
    poll_all(bridge, clock)
    assert mqtt.last('varipro/modbus/status') == 'offline'
    bus.offline = False
    poll_all(bridge, clock)
    assert mqtt.last('varipro/modbus/status') == 'online'


# --- Kirjoitussuoja ---

def test_stale_read_after_write_is_ignored(env):
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    cmd(bridge, 'temp', '22.5')
    assert mqtt.last('varipro/temp/target') == '22.5'
    bus.holding[3] = 215  # laite näyttää vielä vanhaa arvoa
    poll_all(bridge, clock)
    assert mqtt.last('varipro/temp/target') == '22.5'
    clock.now += vb.WRITE_GUARD_S  # suoja vanhenee: laitteen arvo voittaa
    poll_all(bridge, clock)
    assert mqtt.last('varipro/temp/target') == '21.5'


# --- Komennot ---

def test_temp_command_writes_rounded_value(env):
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    cmd(bridge, 'temp', '21.7')
    assert bus.writes[-1] == (vb.REG_TARGET_HOME, 217)
    assert mqtt.last('varipro/temp/target_home') == '21.7'


def test_temp_command_in_away_writes_away_register(env):
    bridge, bus, mqtt, clock = env
    bus.holding[0] = vb.MODE_AWAY
    poll_all(bridge, clock)
    cmd(bridge, 'temp', '23')
    assert bus.writes[-1] == (vb.REG_TARGET_AWAY, 230)
    assert mqtt.last('varipro/temp/target_away') == '23.0'


@pytest.mark.parametrize('payload', ['30', '10', 'abc'])
def test_invalid_temp_rejected(env, payload):
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    cmd(bridge, 'temp', payload)
    assert bus.writes == []
    assert mqtt.last('varipro/temp/target') == '21.5'


def test_invalid_mode_not_echoed(env):
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    cmd(bridge, 'mode', 'heat')
    assert bus.writes == []
    assert mqtt.last('varipro/mode') == 'cool'


def test_fan_off_and_invalid_rejected(env):
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    cmd(bridge, 'fan', 'off')
    cmd(bridge, 'fan', 'turbo')
    assert bus.writes == []
    assert mqtt.last('varipro/fan') == 'auto'
    cmd(bridge, 'fan', 'high')
    assert bus.writes == [(vb.REG_FAN, 3)]
    assert mqtt.last('varipro/fan') == 'high'


def test_failed_write_keeps_state(env):
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    bus.fail_writes.add(vb.REG_FAN)
    cmd(bridge, 'fan', 'high')
    assert mqtt.last('varipro/fan') == 'auto'


def test_preset_none_means_home(env):
    bridge, bus, mqtt, clock = env
    bus.holding[0] = vb.MODE_AWAY
    poll_all(bridge, clock)
    cmd(bridge, 'preset', 'none')
    assert bus.holding[0] == vb.MODE_NORMAL
    assert mqtt.last('varipro/preset') == 'home'
    assert mqtt.last('varipro/temp/target') == '21.5'


def test_cool_keeps_away_and_off_cool_returns_home(env):
    bridge, bus, mqtt, clock = env
    bus.holding[0] = vb.MODE_AWAY
    poll_all(bridge, clock)
    cmd(bridge, 'mode', 'cool')
    assert bus.holding[0] == vb.MODE_AWAY
    cmd(bridge, 'mode', 'off')
    cmd(bridge, 'mode', 'cool')
    assert bus.holding[0] == vb.MODE_NORMAL


@pytest.mark.parametrize('payload', ['-1', '16', 'x'])
def test_invalid_deadzone_rejected(env, payload):
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    cmd(bridge, 'deadzone', payload)
    assert bus.writes == []
    assert mqtt.last('varipro/deadzone') == '2.0'


# --- Fan only ---

def test_fan_only_enter_and_exit(env):
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    cmd(bridge, 'mode', 'fan_only')
    assert bus.holding[0] == vb.MODE_AWAY
    assert bus.holding[201] == vb.FANONLY_DEADZONE_RAW
    assert bus.holding[2] == vb.FAN_TO_RAW['medium']
    assert mqtt.last('varipro/mode') == 'fan_only'
    assert mqtt.last('varipro/temp/target') == 'None'
    assert bridge.store.load()['deadzone_raw'] == 20

    poll_all(bridge, clock)  # 15.0 ei saa tallentua oikeaksi kuolleeksi alueeksi
    assert bridge.deadzone_raw == 20

    cmd(bridge, 'mode', 'cool')
    assert bus.holding[0] == vb.MODE_NORMAL
    assert bus.holding[201] == 20
    assert bus.holding[2] == vb.FAN_TO_RAW['auto']
    assert mqtt.last('varipro/mode') == 'cool'
    assert mqtt.last('varipro/temp/target') == '21.5'
    assert bridge.store.load() is None


def test_fan_only_from_away_returns_to_away(env):
    bridge, bus, mqtt, clock = env
    bus.holding[0] = vb.MODE_AWAY
    poll_all(bridge, clock)
    cmd(bridge, 'mode', 'fan_only')
    cmd(bridge, 'mode', 'cool')
    assert bus.holding[0] == vb.MODE_AWAY
    assert mqtt.last('varipro/preset') == 'away'


def test_fan_only_ended_from_panel(env):
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    cmd(bridge, 'mode', 'fan_only')
    bus.holding[0] = vb.MODE_OFF  # käyttäjä sammutti paneelista
    clock.now += vb.WRITE_GUARD_S
    poll_all(bridge, clock)
    assert not bridge.fan_only_active
    assert bus.holding[201] == 20
    assert bus.holding[2] == vb.FAN_TO_RAW['auto']
    assert mqtt.last('varipro/mode') == 'off'


def test_fan_only_rolls_back_on_partial_failure(env):
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    bus.fail_writes.add(vb.REG_MODE)
    cmd(bridge, 'mode', 'fan_only')
    assert not bridge.fan_only_active
    assert bus.holding[201] == 20
    assert bridge.store.load() is None
    assert mqtt.last('varipro/mode') == 'cool'


def test_fan_only_no_periodic_writes(env):
    """Muuttumattomia arvoja ei kirjoiteta uudelleen, eikä paneelista tehtyä muutosta kumota."""
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    cmd(bridge, 'mode', 'fan_only')
    bus.writes.clear()
    bus.holding[2] = vb.FAN_TO_RAW['high']  # käyttäjä vaihtoi nopeuden paneelista
    for _ in range(10):
        clock.now += 60
        poll_all(bridge, clock)
    assert bus.writes == []
    assert bridge.fan_only_active
    assert mqtt.last('varipro/fan') == 'high'


def test_shutdown_restores_fan_only(env):
    bridge, bus, mqtt, clock = env
    bus.holding[0] = vb.MODE_OFF
    poll_all(bridge, clock)
    cmd(bridge, 'mode', 'fan_only')
    bridge.exit_fan_only(write_mode=True)  # sama kuin shutdown()
    assert bus.holding[0] == vb.MODE_OFF   # palautuu tarkalleen edeltävään tilaan
    assert bus.holding[201] == 20
    assert bus.holding[2] == vb.FAN_TO_RAW['auto']


def test_startup_restore_includes_fan(env):
    bridge, bus, mqtt, clock = env
    # Edellinen ajo kaatui fan onlyn aikana: laite away + 15 °C + medium
    bus.holding.update({0: vb.MODE_AWAY, 201: 150, 2: 2})
    bridge.store.save({'preset': 'home', 'deadzone_raw': 30, 'fan': 'low'})
    bridge.pending_restore = (bridge.store.load(), True)
    poll_all(bridge, clock)
    bridge.retry_pending_restore()
    assert bus.holding[0] == vb.MODE_NORMAL
    assert bus.holding[201] == 30
    assert bus.holding[2] == vb.FAN_TO_RAW['low']
    assert bridge.pending_restore is None
    assert bridge.store.load() is None


def test_startup_restore_respects_panel_change(env):
    bridge, bus, mqtt, clock = env
    bus.holding.update({0: vb.MODE_OFF, 201: 150, 2: 2})
    bridge.store.save({'preset': 'home', 'deadzone_raw': 20, 'fan': 'auto'})
    bridge.pending_restore = (bridge.store.load(), True)
    bridge.retry_pending_restore()
    assert bus.holding[0] == vb.MODE_OFF


def test_failed_exit_retried(env):
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    cmd(bridge, 'mode', 'fan_only')
    bus.fail_writes.add(vb.REG_DEADZONE)
    cmd(bridge, 'mode', 'cool')
    assert bridge.pending_restore is not None
    assert bridge.store.load() is not None  # säilyy myös uudelleenkäynnistyksen yli
    poll_all(bridge, clock)
    assert bridge.deadzone_raw == 20       # 15.0 ei korvaa oikeaa arvoa
    bus.fail_writes.clear()
    bridge.retry_pending_restore()
    assert bus.holding[201] == 20
    assert bridge.pending_restore is None


def test_commands_ignored_in_fan_only(env):
    bridge, bus, mqtt, clock = env
    poll_all(bridge, clock)
    cmd(bridge, 'mode', 'fan_only')
    bus.writes.clear()
    cmd(bridge, 'temp', '22')
    cmd(bridge, 'deadzone', '3')
    cmd(bridge, 'fan', 'auto')
    assert bus.writes == []
    assert mqtt.last('varipro/temp/target') == 'None'


# --- MQTT ja discovery ---

class Msg:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload.encode()


def test_mqtt_messages_queued_not_executed(env):
    bridge, bus, mqtt, clock = env
    bridge.on_mqtt_message(None, None, Msg('varipro/temp/set', '22'))
    bridge.on_mqtt_message(None, None, Msg('homeassistant/status', 'online'))
    bridge.on_mqtt_message(None, None, Msg('muu/topic', 'x'))
    assert bus.writes == []
    assert bridge.commands.get_nowait() == ('temp', '22')
    assert bridge.commands.get_nowait() == ('_ha_online', None)
    assert bridge.commands.empty()


def test_discovery_keeps_v1_unique_ids(env):
    bridge, *_ = env
    configs = dict(bridge.discovery_configs())
    climate = configs['homeassistant/climate/varipro/config']
    assert climate['unique_id'] == 'varipro_chiller'
    assert climate['device']['identifiers'] == ['varipro_001']
    assert 'via_device' not in climate['device']
    assert climate['availability_mode'] == 'all'
    sensor = configs['homeassistant/sensor/varipro_water_in/config']
    assert sensor['entity_category'] == 'diagnostic'
    assert sensor['name'] == 'Tuloveden lämpötila'
    assert 'homeassistant/binary_sensor/varipro_halytys_suodatin/config' in configs
    json.dumps(configs)


def test_topic_prefix_changes_ids(tmp_path):
    cfg = vb.Config(state_dir=str(tmp_path), topic_prefix='vp2')
    bridge = vb.VariProBridge(cfg, bus=FakeBus(), mqtt_client=FakeMqtt())
    configs = dict(bridge.discovery_configs())
    climate = configs['homeassistant/climate/vp2/config']
    assert climate['unique_id'] == 'vp2_chiller'
    assert climate['mode_command_topic'] == 'vp2/mode/set'
    assert bridge.store.path.endswith('vp2_fanonly.json')
