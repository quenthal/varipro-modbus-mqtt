#!/usr/bin/env python3
"""
VariPro Modbus-MQTT silta
- Lukee peräkkäiset rekisterit yhdellä kyselyllä, porrastetut pollausvälit
- Kaikki väyläliikenne yhdessä säikeessä, vähintään 200 ms kyselyjen välissä
- Kirjoitussuoja: kirjoituksen jälkeen laitteelta luettu vanha arvo ohitetaan
- Preset-tuki home/away (erilliset tavoitelämpötilarekisterit 3 ja 4)
- Action state jäähdytyksen/lämmityksen PID-lähdöstä (rekisterit 9 ja 10)
- Hälytysbittien purku binary-sensoreiksi
- Away-tilan kuollut alue säädettävänä (holding 201 / 4x00202, skaalaus x0.1 varmistettu)
- Fan only -tila (synteettinen: away + kuollut alue 15 °C, tilannekuva levylle)
- Availability: silta (MQTT LWT) ja Modbus-yhteys erikseen

Laite: Chiller VariPro -huonesäädin (GRAND/BOX/STUDIO VariPro -puhallinkonvektorit)
Rekisterikartta: VariPro-käyttöliittymän rekisterikartta v1.4 (N03104128)

Konfiguraatio ympäristömuuttujilla, ks. README.md tai Config.from_env().

Lisenssi: MIT. Kehitetty iteratiivisesti Claude-tekoälyn avustuksella.
"""

import json
import logging
import os
import queue
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime

__version__ = "2.0.0"

logger = logging.getLogger("varipro")

# Holding-rekisterit (0-pohjaiset, 4x00001 = 0)
REG_MODE = 0
REG_FAN = 2
REG_TARGET_HOME = 3
REG_TARGET_AWAY = 4
REG_DEADZONE = 201

# Sovellustila (rekisteri 0)
MODE_OFF = 1
MODE_NORMAL = 2
MODE_AWAY = 3
MODE_TO_HVAC = {MODE_OFF: 'off', MODE_NORMAL: 'cool', MODE_AWAY: 'cool'}

FAN_TO_RAW = {'off': 0, 'low': 1, 'medium': 2, 'high': 3, 'auto': 4}
RAW_TO_FAN = {v: k for k, v in FAN_TO_RAW.items()}

APP_STATE_TEXT = {0: 'Alustus', 1: 'Valmiustila', 2: 'Normaali', 3: 'Poissa',
                  4: 'Huuhtelu', 5: 'Pysähtynyt', 10: 'Testitila'}
FAN_ACTUAL_TEXT = {0: 'Pois', 1: 'Matala', 2: 'Keskitaso', 3: 'Korkea', 4: 'Auto'}

# Hälytysbitit (3x00103): (bitti, avain, nimi)
ALARM_BITS = [
    (0,  'kondenssi',          'Kondenssianturin hälytys'),
    (1,  'puhallin1_rpm',      'Puuttuva kierrosluku, puhallin 1'),
    (2,  'puhallin2_rpm',      'Puuttuva kierrosluku, puhallin 2'),
    (3,  'anturi_vesi_sisaan', 'Anturivika, vesi sisään'),
    (4,  'anturi_vesi_ulos',   'Anturivika, vesi ulos'),
    (11, 'parametritiedosto',  'Parametritiedosto'),
    (15, 'suodatin',           'Suodattimen puhdistus'),
]

FANONLY_DEADZONE_RAW = 150   # 15.0 °C
DEFAULT_DEADZONE_RAW = 20    # 2.0 °C, jos oikeaa arvoa ei ole vielä luettu
WRITE_GUARD_S = 15           # näin kauan kirjoituksen jälkeen eroava lukema tulkitaan vanhaksi
MODBUS_FAIL_LIMIT = 3        # peräkkäiset lukuvirheet ennen kuin Modbus merkitään offline-tilaan

REPO_URL = "https://github.com/quenthal/varipro-modbus-mqtt"


def scale10(raw):
    return round(raw * 0.1, 1)


def to_raw10(value):
    return int(round(float(value) * 10))


# ----------------------------------------------------------------------
# Konfiguraatio
# ----------------------------------------------------------------------

@dataclass
class Config:
    modbus_host: str = ''  # pakollinen
    modbus_port: int = 502
    modbus_timeout: float = 5.0
    slave: int = 1
    mqtt_host: str = 'localhost'
    mqtt_port: int = 1883
    mqtt_user: str = ''
    mqtt_pass: str = ''
    topic_prefix: str = 'varipro'
    discovery_prefix: str = 'homeassistant'
    min_temp: float = 18.0
    max_temp: float = 24.0
    deadzone_max: float = 15.0
    state_dir: str = ''
    log_level: str = 'INFO'

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env

        def get(name, default, conv=str):
            value = env.get(f'VARIPRO_{name}')
            return default if value in (None, '') else conv(value)

        d = cls()
        return cls(
            modbus_host=get('MODBUS_HOST', d.modbus_host),
            modbus_port=get('MODBUS_PORT', d.modbus_port, int),
            modbus_timeout=get('MODBUS_TIMEOUT', d.modbus_timeout, float),
            slave=get('SLAVE', d.slave, int),
            mqtt_host=get('MQTT_HOST', d.mqtt_host),
            mqtt_port=get('MQTT_PORT', d.mqtt_port, int),
            mqtt_user=get('MQTT_USER', d.mqtt_user),
            mqtt_pass=get('MQTT_PASS', d.mqtt_pass),
            topic_prefix=get('TOPIC_PREFIX', d.topic_prefix),
            discovery_prefix=get('DISCOVERY_PREFIX', d.discovery_prefix),
            min_temp=get('MIN_TEMP', d.min_temp, float),
            max_temp=get('MAX_TEMP', d.max_temp, float),
            deadzone_max=get('DEADZONE_MAX', d.deadzone_max, float),
            # systemd:n StateDirectory= asettaa STATE_DIRECTORY-muuttujan
            state_dir=(get('STATE_DIR', '') or env.get('STATE_DIRECTORY', '')
                       or os.path.dirname(os.path.abspath(__file__))),
            log_level=get('LOG_LEVEL', d.log_level).upper(),
        )


# ----------------------------------------------------------------------
# Modbus-väylä
# ----------------------------------------------------------------------

class ModbusBus:
    """Ohut kääre pymodbus-asiakkaan ympärille: virheet palautetaan None/False-
    arvoina (ei poikkeuksia), ja kyselyjen väliin jätetään vähintään min_gap
    sekuntia (manuaali: >= 200 ms). Käytetään vain pääsäikeestä."""

    def __init__(self, host, port, device_id, timeout, min_gap=0.2, client=None):
        if client is None:
            from pymodbus.client import ModbusTcpClient
            client = ModbusTcpClient(host, port=port, timeout=timeout, retries=2)
        self.client = client
        self.device_id = device_id
        self.min_gap = min_gap
        self.last_error = None
        self._last_request = 0.0

    def _request(self, func, *args, **kwargs):
        delay = self._last_request + self.min_gap - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        try:
            result = func(*args, device_id=self.device_id, **kwargs)
        except Exception as e:
            self.last_error = str(e) or type(e).__name__
            self.client.close()  # seuraava kysely avaa yhteyden uudelleen
            return None
        finally:
            self._last_request = time.monotonic()
        if result is None or result.isError():
            self.last_error = str(result)
            return None
        self.last_error = None
        return result

    def read(self, kind, start, count):
        func = (self.client.read_holding_registers if kind == 'holding'
                else self.client.read_input_registers)
        result = self._request(func, start, count=count)
        return None if result is None else list(result.registers)

    def write(self, addr, value):
        return self._request(self.client.write_register, addr, value) is not None

    def close(self):
        self.client.close()


# ----------------------------------------------------------------------
# Fan only -tilannekuva
# ----------------------------------------------------------------------

class FanOnlyStore:
    """Fan only -tilannekuva levyllä: tila ennen fan onlya, jotta se voidaan
    palauttaa myös kaatumisen tai uudelleenkäynnistyksen jälkeen."""

    def __init__(self, path):
        self.path = path

    def save(self, snapshot):
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, 'w') as f:
                json.dump(snapshot, f)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.error(f"Fan only -tilannekuvan tallennus epäonnistui: {e}")

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path) as f:
                    return json.load(f)
        except (OSError, ValueError) as e:
            logger.error(f"Fan only -tilannekuvan luku epäonnistui: {e}")
        return None

    def clear(self):
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except OSError as e:
            logger.error(f"Fan only -tilannekuvan poisto epäonnistui: {e}")


@dataclass
class RegisterGroup:
    name: str
    kind: str        # 'holding' tai 'input'
    start: int
    count: int
    interval: float  # s
    handlers: dict   # {osoite: käsittelijä(raw)}


# ----------------------------------------------------------------------
# Silta
# ----------------------------------------------------------------------

class VariProBridge:
    def __init__(self, config, bus=None, mqtt_client=None, clock=time.monotonic):
        self.cfg = config
        self.prefix = config.topic_prefix
        self.clock = clock

        self.bus = bus or ModbusBus(config.modbus_host, config.modbus_port,
                                    config.slave, config.modbus_timeout)
        self.mqtt = mqtt_client or self._make_mqtt_client()

        # Laitteen tila (None = ei vielä luettu)
        self.mode_raw = None
        self.fan = None
        self.target_home = None
        self.target_away = None
        self.deadzone_raw = None
        self.cooling_pid = None
        self.heating_pid = None
        self.fan_speed = None

        # Fan only
        self.fan_only_active = False
        self.fan_only_snapshot = None
        self.store = FanOnlyStore(os.path.join(config.state_dir, f'{self.prefix}_fanonly.json'))
        # (tilannekuva, palautetaanko sovellustila) - laitteelle palauttamatta oleva tila
        self.pending_restore = None

        self.published = {}       # {alitopic: payload} - julkaistaan vain muutokset
        self.write_guard = {}     # {rekisteri: (raw, voimassa asti)}
        self.last_poll = {}
        self.modbus_failures = 0
        self.modbus_online = None

        self.commands = queue.SimpleQueue()  # put() on turvallinen signaalinkäsittelijästä
        self.stop_event = threading.Event()

        self.register_groups = [
            RegisterGroup('holding_main', 'holding', 0, 5, 10, {
                REG_MODE: self._on_mode,
                REG_FAN: self._on_fan,
                REG_TARGET_HOME: self._on_target_home,
                REG_TARGET_AWAY: self._on_target_away,
            }),
            RegisterGroup('holding_deadzone', 'holding', REG_DEADZONE, 1, 60, {
                REG_DEADZONE: self._on_deadzone,
            }),
            RegisterGroup('input_main', 'input', 3, 8, 10, {
                3: self._on_app_state,                                          # 3x00004 Sovellustila
                5: self._on_fan_actual,                                         # 3x00006 Puhaltimen säätö
                6: lambda raw: self.publish('temp/current', scale10(raw)),      # 3x00007 Huonelämpötila
                7: lambda raw: self.publish('temp/setpoint_active', scale10(raw)),  # 3x00008 Asetusarvo
                8: self._on_fan_speed,                                          # 3x00009 Puhallin %
                9: self._on_cooling_pid,                                        # 3x00010 Jäähdytys-PID %
                10: self._on_heating_pid,                                       # 3x00011 Lämmitys-PID %
            }),
            RegisterGroup('input_water', 'input', 115, 2, 30, {
                115: lambda raw: self.publish('water_in', scale10(raw)),       # 3x00116
                116: lambda raw: self.publish('water_out', scale10(raw)),      # 3x00117
            }),
            RegisterGroup('alarm', 'input', 102, 1, 60, {
                102: self._on_alarm,                                            # 3x00103 bittikenttä
            }),
        ]

    def topic(self, sub):
        return f"{self.prefix}/{sub}"

    # ------------------------------------------------------------------
    # MQTT
    # ------------------------------------------------------------------

    def _make_mqtt_client(self):
        import paho.mqtt.client as mqtt
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{self.prefix}_bridge")
        if self.cfg.mqtt_user:
            client.username_pw_set(self.cfg.mqtt_user, self.cfg.mqtt_pass)
        client.will_set(self.topic('bridge/status'), 'offline', qos=1, retain=True)
        client.on_connect = self.on_mqtt_connect
        client.on_message = self.on_mqtt_message
        return client

    def publish(self, sub, value, force=False):
        payload = str(value)
        if not force and self.published.get(sub) == payload:
            return
        self.mqtt.publish(self.topic(sub), payload, retain=True)
        self.published[sub] = payload

    def republish_all(self):
        for sub, payload in list(self.published.items()):
            self.mqtt.publish(self.topic(sub), payload, retain=True)

    # Callbackit ajetaan pahon säikeessä: ne vain jonottavat työn pääsäikeelle.

    def on_mqtt_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            logger.error(f"MQTT-yhteys hylätty: {reason_code}")
            return
        client.subscribe([(self.topic('+/set'), 0), (f"{self.cfg.discovery_prefix}/status", 0)])
        logger.info("MQTT connected")
        self.commands.put(('_connected', None))

    def on_mqtt_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors='replace')
        if msg.topic == f"{self.cfg.discovery_prefix}/status":
            if payload == 'online':
                self.commands.put(('_ha_online', None))
            return
        head, tail = f"{self.prefix}/", '/set'
        if msg.topic.startswith(head) and msg.topic.endswith(tail):
            self.commands.put((msg.topic[len(head):-len(tail)], payload))

    # ------------------------------------------------------------------
    # Väylä ja kirjoitussuoja
    # ------------------------------------------------------------------

    def write(self, addr, raw):
        if not self.bus.write(addr, raw):
            logger.error(f"Kirjoitus epäonnistui: rekisteri {addr} = {raw} ({self.bus.last_error})")
            return False
        self.write_guard[addr] = (raw, self.clock() + WRITE_GUARD_S)
        return True

    def guard_blocks(self, addr, raw):
        """True, jos lukema on todennäköisesti vanha arvo juuri kirjoitetun tilalla."""
        guard = self.write_guard.get(addr)
        if guard is None:
            return False
        written, until = guard
        if raw == written or self.clock() >= until:
            del self.write_guard[addr]
            return False
        logger.debug(f"Ohitetaan rekisterin {addr} lukema {raw} (kirjoitettu {written})")
        return True

    def set_modbus_online(self, online):
        if online == self.modbus_online:
            return
        self.modbus_online = online
        self.publish('modbus/status', 'online' if online else 'offline', force=True)
        if online:
            logger.info("Modbus-yhteys kunnossa")
        else:
            logger.warning(f"Modbus-yhteys poikki ({self.bus.last_error})")

    # ------------------------------------------------------------------
    # Pollaus
    # ------------------------------------------------------------------

    def poll_due_groups(self):
        for group in self.register_groups:
            last = self.last_poll.get(group.name)
            if last is not None and self.clock() - last < group.interval:
                continue
            if not self.read_group(group):
                break  # väylä ei vastaa - ei jäädä odottamaan jokaisen ryhmän aikakatkaisua
            self.process_commands()

    def read_group(self, group):
        registers = self.bus.read(group.kind, group.start, group.count)
        if registers is None:
            self.modbus_failures += 1
            logger.debug(f"Error reading {group.name}: {self.bus.last_error}")
            if self.modbus_failures >= MODBUS_FAIL_LIMIT:
                self.set_modbus_online(False)
            return False
        self.modbus_failures = 0
        self.set_modbus_online(True)
        self.last_poll[group.name] = self.clock()

        for addr, handler in group.handlers.items():
            raw = registers[addr - group.start]
            if group.kind == 'holding' and self.guard_blocks(addr, raw):
                continue
            handler(raw)
        self.publish_derived()
        return True

    def _on_mode(self, raw):
        self.mode_raw = raw
        if self.fan_only_active and raw != MODE_AWAY:
            logger.warning(f"Fan only keskeytetty ulkopuolisella tilamuutoksella (rekisteri 0 = {raw})")
            self.exit_fan_only(write_mode=False)

    def _on_fan(self, raw):
        self.fan = RAW_TO_FAN.get(raw, 'auto')

    def _on_target_home(self, raw):
        self.target_home = scale10(raw)
        self.publish('temp/target_home', self.target_home)

    def _on_target_away(self, raw):
        self.target_away = scale10(raw)
        self.publish('temp/target_away', self.target_away)

    def _on_deadzone(self, raw):
        if self.published.get('deadzone') != str(scale10(raw)):
            logger.info(f"Kuollut alue raaka-arvo: {raw} -> {scale10(raw)} °C")
        # Fan onlyn (tai kesken jääneen palautuksen) 15.0 ei ole "oikea" arvo
        if not self.fan_only_active and self.pending_restore is None:
            self.deadzone_raw = raw
        self.publish('deadzone', scale10(raw))

    def _on_app_state(self, raw):
        self.publish('app_state', raw)
        self.publish('app_state_text', APP_STATE_TEXT.get(raw, f'Tuntematon ({raw})'))

    def _on_fan_actual(self, raw):
        self.publish('fan_actual', raw)
        self.publish('fan_actual_text', FAN_ACTUAL_TEXT.get(raw, f'Tuntematon ({raw})'))

    def _on_fan_speed(self, raw):
        self.fan_speed = raw
        self.publish('fan_speed', raw)

    def _on_cooling_pid(self, raw):
        self.cooling_pid = raw
        self.publish('cooling_pid', raw)

    def _on_heating_pid(self, raw):
        self.heating_pid = raw
        self.publish('heating_pid', raw)

    def _on_alarm(self, raw):
        self.publish('alarm', raw)
        for bit, key, _name in ALARM_BITS:
            state = 'ON' if raw & (1 << bit) else 'OFF'
            if state == 'ON' and self.published.get(f'alarm/{key}') != 'ON':
                logger.warning(f"Hälytys aktiivinen: {key}")
            self.publish(f'alarm/{key}', state)

    # ------------------------------------------------------------------
    # Johdetut tilat: mode, preset, aktiivinen tavoite, action
    # ------------------------------------------------------------------

    @property
    def preset(self):
        return 'away' if self.mode_raw == MODE_AWAY else 'home'

    def active_target(self):
        return self.target_away if self.preset == 'away' else self.target_home

    def determine_action(self):
        if not self.fan_only_active and self.mode_raw == MODE_OFF:
            return 'off'
        if (self.cooling_pid or 0) > 0:
            return 'cooling'
        if (self.heating_pid or 0) > 0:
            return 'heating'
        if (self.fan_speed or 0) > 4:
            return 'fan'
        return 'idle'

    def publish_derived(self):
        if self.mode_raw is not None:
            if self.fan_only_active:
                self.publish('mode', 'fan_only')
                # Fan onlyssa ei ole tavoitelämpöä - None piilottaa kortin lämpötilasäätimen.
                # Preset pysyy jäädytettynä fan onlya edeltävään arvoon.
                self.publish('temp/target', 'None')
            else:
                self.publish('mode', MODE_TO_HVAC.get(self.mode_raw, 'off'))
                self.publish('preset', self.preset)
                if self.active_target() is not None:
                    self.publish('temp/target', self.active_target())
        if self.fan is not None:
            self.publish('fan', self.fan)
        if self.cooling_pid is not None:
            self.publish('action', self.determine_action())

    # ------------------------------------------------------------------
    # Fan only
    # ------------------------------------------------------------------

    @staticmethod
    def snapshot_mode(snapshot):
        mode_raw = snapshot.get('mode_raw')
        if mode_raw in MODE_TO_HVAC:
            return mode_raw
        return MODE_AWAY if snapshot.get('preset') == 'away' else MODE_NORMAL

    def enter_fan_only(self):
        """Siirry fan only -tilaan: away + kuollut alue 15 °C. Epäonnistuessa
        perutaan jo tehdyt kirjoitukset, jotta laite ei jää välitilaan."""
        if self.mode_raw is None or self.pending_restore is not None:
            logger.warning("Fan only ei mahdollinen nyt (laitteen tila tuntematon tai palautus kesken)")
            self.republish_rejected('mode')
            return False

        deadzone_raw = self.deadzone_raw if self.deadzone_raw is not None else DEFAULT_DEADZONE_RAW
        snapshot = {
            'preset': self.preset,
            'mode_raw': self.mode_raw,
            'deadzone_raw': deadzone_raw,
            'fan': self.fan or 'auto',
            'time': datetime.now().isoformat(timespec='seconds'),
        }
        self.store.save(snapshot)  # ennen kirjoituksia: kaatuminen kesken on palautettavissa

        if not self.write(REG_DEADZONE, FANONLY_DEADZONE_RAW):
            self.store.clear()
            self.republish_rejected('mode')
            return False
        if not self.write(REG_MODE, MODE_AWAY):
            if not self.write(REG_DEADZONE, deadzone_raw):
                self.pending_restore = (snapshot, False)  # store säilyy: yritetään uudelleen
            else:
                self.store.clear()
            self.republish_rejected('mode')
            return False
        self.mode_raw = MODE_AWAY

        # Puhallin manuaalille jos auto/off (auto pysäyttäisi puhaltimen ilman jäähdytystarvetta)
        if snapshot['fan'] in ('auto', 'off'):
            if self.write(REG_FAN, FAN_TO_RAW['medium']):
                self.fan = 'medium'

        self.fan_only_active = True
        self.fan_only_snapshot = snapshot
        self.publish('deadzone', scale10(FANONLY_DEADZONE_RAW))
        self.publish_derived()
        logger.info(f"Fan only PÄÄLLE (tilannekuva: {snapshot})")
        return True

    def restore_snapshot(self, snapshot, write_mode):
        """Kirjoita tilannekuvan arvot laitteelle. True, jos kaikki onnistui.
        write_mode=True palauttaa myös sovellustilan, mutta vain jos laite on
        yhä away-tilassa (paneelista tehtyä muutosta ei kumota)."""
        ok = True
        deadzone_raw = snapshot.get('deadzone_raw', DEFAULT_DEADZONE_RAW)
        if self.write(REG_DEADZONE, deadzone_raw):
            self.deadzone_raw = deadzone_raw
            self.publish('deadzone', scale10(deadzone_raw))
        else:
            ok = False

        if write_mode:
            regs = self.bus.read('holding', REG_MODE, 1)
            if regs is None:
                ok = False
            elif regs[0] == MODE_AWAY:
                mode_raw = self.snapshot_mode(snapshot)
                if mode_raw != MODE_AWAY and not self.write(REG_MODE, mode_raw):
                    ok = False
                else:
                    self.mode_raw = mode_raw

        # Fan only on "kupla": myös enter-vaiheen medium-pakotus ja käyttäjän
        # fan onlyn aikana tekemät vaihdot perutaan
        orig_fan = snapshot.get('fan')
        if orig_fan in FAN_TO_RAW and orig_fan != self.fan:
            if self.write(REG_FAN, FAN_TO_RAW[orig_fan]):
                self.fan = orig_fan
                logger.info(f"Puhallin palautettu: {orig_fan}")
            else:
                ok = False
        return ok

    def exit_fan_only(self, write_mode=True):
        """Poistu fan only -tilasta ja palauta tilannekuva. Palauttaa fan onlya
        edeltäneen presetin. write_mode=False jos tila muuttui jo muualta
        (paneeli) tai kutsuja kirjoittaa sovellustilan itse (mode/preset-komento)."""
        snapshot = self.fan_only_snapshot or self.store.load() or {}
        self.fan_only_active = False
        self.fan_only_snapshot = None

        if self.restore_snapshot(snapshot, write_mode):
            self.store.clear()
        else:
            logger.error("Fan only -palautus jäi kesken - yritetään uudelleen")
            self.pending_restore = (snapshot, write_mode)
        logger.info(f"Fan only POIS (preset ennen: {snapshot.get('preset', 'home')}, "
                    f"kuollut alue raw: {snapshot.get('deadzone_raw')})")
        return snapshot.get('preset', 'home')

    def retry_pending_restore(self):
        snapshot, write_mode = self.pending_restore
        if self.restore_snapshot(snapshot, write_mode):
            logger.info("Fan only -tilannekuva palautettu laitteelle")
            self.pending_restore = None
            self.store.clear()
            self.publish_derived()

    # ------------------------------------------------------------------
    # Komennot (pääsäikeessä)
    # ------------------------------------------------------------------

    def process_commands(self, timeout=0.0):
        """Käsittele jonossa olevat komennot. timeout > 0 odottaa ensimmäistä."""
        while True:
            try:
                item = self.commands.get(timeout=timeout) if timeout > 0 else self.commands.get_nowait()
            except queue.Empty:
                return
            timeout = 0.0
            if item is None:  # stop()
                return
            cmd, payload = item
            try:
                self.handle_command(cmd, payload)
            except Exception:
                logger.exception(f"Command error ({cmd})")

    def handle_command(self, cmd, payload):
        if cmd == '_connected':
            self.publish_discovery()
            self.mqtt.publish(self.topic('bridge/status'), 'online', qos=1, retain=True)
            self.republish_all()
        elif cmd == '_ha_online':
            logger.info("Home Assistant käynnistyi - julkaistaan discovery ja tilat uudelleen")
            self.publish_discovery()
            self.republish_all()
        elif cmd == 'mode':
            self.cmd_mode(payload)
        elif cmd == 'fan':
            self.cmd_fan(payload)
        elif cmd == 'temp':
            self.cmd_temp(payload)
        elif cmd == 'preset':
            self.cmd_preset(payload)
        elif cmd == 'deadzone':
            self.cmd_deadzone(payload)
        else:
            logger.warning(f"Tuntematon komento: {cmd}")

    def republish_rejected(self, sub):
        """Hylätty tai epäonnistunut komento: lähetä nykyinen tila uudelleen,
        jotta HA:n käyttöliittymä palaa todelliseen arvoon."""
        if sub in self.published:
            self.publish(sub, self.published[sub], force=True)

    def cmd_mode(self, payload):
        if payload not in ('off', 'cool', 'fan_only'):
            logger.warning(f"Tuntematon mode '{payload}' - ohitetaan")
            self.republish_rejected('mode')
            return
        if payload == 'fan_only':
            if not self.fan_only_active:
                self.enter_fan_only()
            return

        # cool tai off - poistu ensin fan onlysta jos päällä
        restored_preset = None
        if self.fan_only_active:
            restored_preset = self.exit_fan_only(write_mode=False)

        if payload == 'off':
            value = MODE_OFF
        elif restored_preset == 'away' or (restored_preset is None and self.mode_raw == MODE_AWAY):
            value = MODE_AWAY  # cool pitää away-viilennyksen (myös fan onlysta palatessa)
        else:
            value = MODE_NORMAL

        ok = self.write(REG_MODE, value)
        if ok:
            self.mode_raw = value
            logger.debug(f"Set mode to {payload} (register {value})")
        self.publish_derived()
        if not ok:
            self.republish_rejected('mode')

    def cmd_fan(self, payload):
        # 'off' ei ole valikossa: laite ei pidä fan-rekisterin arvoa 0 (palauttaa
        # auton), ja se olisi joka tapauksessa sama kuin koko laite off.
        if payload == 'off' or payload not in FAN_TO_RAW:
            logger.info(f"Fan '{payload}' ei tuettu - ohitetaan")
        elif self.fan_only_active and payload == 'auto':
            logger.info("Fan auto ei ole mielekäs fan only -tilassa - ohitetaan")
        elif self.write(REG_FAN, FAN_TO_RAW[payload]):
            self.fan = payload
            self.publish('fan', payload)
            logger.debug(f"Set fan to {payload}")
            return
        self.republish_rejected('fan')

    def cmd_temp(self, payload):
        if self.fan_only_active:
            logger.info("Lämpötilakomento ohitettu fan only -tilassa")
            self.republish_rejected('temp/target')
            return
        try:
            value = float(payload)
        except ValueError:
            value = None
        if value is None or not self.cfg.min_temp <= value <= self.cfg.max_temp:
            logger.warning(f"Tavoitelämpö '{payload}' ei kelpaa "
                           f"({self.cfg.min_temp}-{self.cfg.max_temp} °C) - ohitetaan")
            self.republish_rejected('temp/target')
            return

        raw = to_raw10(value)
        away = self.preset == 'away'
        register = REG_TARGET_AWAY if away else REG_TARGET_HOME
        if not self.write(register, raw):
            self.republish_rejected('temp/target')
            return
        if away:
            self.target_away = scale10(raw)
            self.publish('temp/target_away', self.target_away)
        else:
            self.target_home = scale10(raw)
            self.publish('temp/target_home', self.target_home)
        self.publish_derived()
        logger.debug(f"Set temp to {scale10(raw)}°C on register {register} ({self.preset})")

    def cmd_preset(self, payload):
        # Preset-valinta fan onlyn aikana = poistuminen cool-tilaan
        if self.fan_only_active:
            self.exit_fan_only(write_mode=False)

        # HA lisää preset-valikkoon aina "none"-vaihtoehdon: tulkitaan se homeksi,
        # ja julkaistaan "home" takaisin, jotta UI ei jää näyttämään "Ei mitään".
        if payload not in ('home', 'away', 'none'):
            logger.warning(f"Tuntematon preset '{payload}' - tulkitaan homeksi")
        value = MODE_AWAY if payload == 'away' else MODE_NORMAL
        if self.mode_raw == MODE_OFF:
            logger.warning(f"Preset {payload} requested while device is off - device will turn on")

        ok = self.write(REG_MODE, value)
        if ok:
            self.mode_raw = value
            logger.debug(f"Set preset to {self.preset} (requested: {payload})")
        self.publish_derived()
        if not ok or payload != self.preset:
            self.republish_rejected('preset')  # myös "none" -> "home"

    def cmd_deadzone(self, payload):
        if self.fan_only_active:
            # Kuollut alue on fan onlyn käytössä - älä anna sotkea
            logger.info("Kuollut alue -komento ohitettu fan only -tilassa")
            self.republish_rejected('deadzone')
            return
        try:
            value = float(payload)
        except ValueError:
            value = None
        if value is None or not 0 <= value <= self.cfg.deadzone_max:
            logger.warning(f"Kuollut alue '{payload}' ei kelpaa (0-{self.cfg.deadzone_max} °C) - ohitetaan")
        else:
            raw = to_raw10(value)
            if self.write(REG_DEADZONE, raw):
                self.deadzone_raw = raw
                self.publish('deadzone', scale10(raw))
                logger.info(f"Set deadzone to {scale10(raw)}°C (register {REG_DEADZONE} = {raw})")
                return
        self.republish_rejected('deadzone')

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def uid(self, name):
        return f"{self.prefix}_{name}"

    def discovery_configs(self):
        """[(config-topic, payload)]. unique_id:t ja oletustopicit ovat samat kuin
        v1:ssä, joten HA:n entiteetit (ja historia) säilyvät päivityksessä."""
        disc = self.cfg.discovery_prefix
        device = {
            "identifiers": ["varipro_001" if self.prefix == 'varipro' else self.prefix],
            "name": "VariPro Chiller",
            "model": "Room Controller",
            "manufacturer": "Chiller",
        }
        origin = {"name": "varipro-modbus-mqtt", "sw_version": __version__, "support_url": REPO_URL}
        availability = {
            "availability": [{"topic": self.topic('bridge/status')},
                             {"topic": self.topic('modbus/status')}],
            "availability_mode": "all",
        }
        common = {"device": device, "origin": origin, **availability}
        configs = []

        climate = {
            "name": "Ilmastointi",
            "unique_id": "varipro_chiller" if self.prefix == 'varipro' else self.uid('chiller'),
            **common,
            "current_temperature_topic": self.topic('temp/current'),
            "temperature_state_topic": self.topic('temp/target'),
            "temperature_command_topic": self.topic('temp/set'),
            "mode_state_topic": self.topic('mode'),
            "mode_command_topic": self.topic('mode/set'),
            "modes": ["off", "cool", "fan_only"],
            "action_topic": self.topic('action'),
            "fan_mode_state_topic": self.topic('fan'),
            "fan_mode_command_topic": self.topic('fan/set'),
            "fan_modes": ["auto", "low", "medium", "high"],
            "preset_mode_state_topic": self.topic('preset'),
            "preset_mode_command_topic": self.topic('preset/set'),
            "preset_modes": ["home", "away"],
            "temp_step": 0.5,
            "min_temp": self.cfg.min_temp,
            "max_temp": self.cfg.max_temp,
        }
        configs.append((f"{disc}/climate/{self.prefix}/config", climate))

        # (topic, nimi, yksikkö, device_class, diagnostiikka)
        sensors = [
            ("temp/current", "Huonelämpötila", "°C", "temperature", False),
            ("fan_speed", "Puhaltimen nopeus", "%", None, True),
            ("cooling_pid", "Jäähdytysteho (PID)", "%", None, True),
            ("heating_pid", "Lämmitysteho (PID)", "%", None, True),
            ("water_in", "Tuloveden lämpötila", "°C", "temperature", True),
            ("water_out", "Poistoveden lämpötila", "°C", "temperature", True),
            ("temp/setpoint_active", "Aktiivinen asetusarvo (laite)", "°C", "temperature", True),
            ("temp/target_home", "Tavoitelämpötila, kotona", "°C", "temperature", True),
            ("temp/target_away", "Tavoitelämpötila, poissa", "°C", "temperature", True),
            ("app_state_text", "Sovellustila", None, None, True),
            ("fan_actual_text", "Puhaltimen tila (toteutunut)", None, None, True),
            ("alarm", "Hälytys (raaka)", None, None, True),
        ]
        for topic, name, unit, dev_class, diagnostic in sensors:
            uid = self.uid(topic.replace('/', '_'))
            config = {"name": name, "unique_id": uid, "state_topic": self.topic(topic), **common}
            if unit:
                config["unit_of_measurement"] = unit
                config["state_class"] = "measurement"  # HA:n pitkäaikaistilastot
            if dev_class:
                config["device_class"] = dev_class
            if diagnostic:
                config["entity_category"] = "diagnostic"
            configs.append((f"{disc}/sensor/{uid}/config", config))

        for _bit, key, name in ALARM_BITS:
            uid = self.uid(f"halytys_{key}")
            configs.append((f"{disc}/binary_sensor/{uid}/config", {
                "name": name,
                "unique_id": uid,
                "state_topic": self.topic(f'alarm/{key}'),
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "problem",
                **common,
            }))

        configs.append((f"{disc}/number/{self.uid('deadzone')}/config", {
            "name": "Poissa-tilan kuollut alue",
            "unique_id": self.uid('deadzone'),
            "state_topic": self.topic('deadzone'),
            "command_topic": self.topic('deadzone/set'),
            "min": 0,
            "max": self.cfg.deadzone_max,
            "step": 0.5,
            "unit_of_measurement": "°C",
            "icon": "mdi:thermometer-lines",
            "entity_category": "config",
            **common,
        }))
        return configs

    def publish_discovery(self):
        for topic, config in self.discovery_configs():
            self.mqtt.publish(topic, json.dumps(config), retain=True, qos=1)

    # ------------------------------------------------------------------
    # Pääsilmukka
    # ------------------------------------------------------------------

    def stop(self, *_args):
        self.stop_event.set()
        self.commands.put(None)

    def run(self):
        logger.info(f"VariPro-silta {__version__}: Modbus {self.cfg.modbus_host}:{self.cfg.modbus_port}, "
                    f"MQTT {self.cfg.mqtt_host}:{self.cfg.mqtt_port}")
        self.mqtt.connect_async(self.cfg.mqtt_host, self.cfg.mqtt_port)
        self.mqtt.loop_start()

        # Jos edellinen ajo päättyi kesken fan onlyn, palautetaan normaalitila
        snapshot = self.store.load()
        if snapshot:
            logger.warning("Fan only -tilannekuva löytyi käynnistyksessä - palautetaan normaalitila")
            self.pending_restore = (snapshot, True)

        try:
            while not self.stop_event.is_set():
                try:
                    self.process_commands()
                    self.poll_due_groups()
                    # Vasta kun väylä on vastannut: katkoksen aikana ei yritetä joka sekunti
                    if self.pending_restore is not None and self.modbus_online:
                        self.retry_pending_restore()
                    self.process_commands(timeout=1.0)
                except Exception:
                    logger.exception("Main loop error")
                    self.stop_event.wait(5)
        finally:
            self.shutdown()

    def shutdown(self):
        # Siisti lopetus: jos fan only päällä, palauta normaalitila
        if self.fan_only_active:
            try:
                self.exit_fan_only(write_mode=True)
            except Exception:
                logger.exception("Fan only -palautus lopetuksessa epäonnistui")
        info = self.mqtt.publish(self.topic('bridge/status'), 'offline', qos=1, retain=True)
        try:
            info.wait_for_publish(timeout=2)
        except Exception:
            pass
        self.mqtt.disconnect()
        self.mqtt.loop_stop()
        self.bus.close()
        logger.info("Silta pysäytetty")


def main():
    config = Config.from_env()
    if not config.modbus_host:
        raise SystemExit("VARIPRO_MODBUS_HOST puuttuu (Modbus TCP -yhdyskäytävän osoite)")
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    bridge = VariProBridge(config)
    signal.signal(signal.SIGTERM, bridge.stop)
    signal.signal(signal.SIGINT, bridge.stop)
    bridge.run()


if __name__ == "__main__":
    main()
