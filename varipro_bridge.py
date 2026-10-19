#!/usr/bin/env python3
"""
VariPro Modbus-MQTT silta
- Lukee peräkkäiset rekisterit yhdellä kyselyllä, porrastetut pollausvälit
- Välimuisti + kevyt kirjoitussuojaus pomppimisen vähentämiseksi
- Preset-tuki home/away (erilliset tavoitelämpötilarekisterit 3 ja 4)
- Action state jäähdytyksen PID-lähdöstä (rekisteri 9)
- Hälytysbittien purku binary-sensoreiksi
- Away-tilan kuollut alue säädettävänä (holding 201 / 4x00202, skaalaus x0.1 varmistettu)
- Fan only -tila (synteettinen: away + kuollut alue 15 °C, tilannekuva levylle)

Laite: Chiller VariPro -huonesäädin (GRAND/BOX/STUDIO VariPro -puhallinkonvektorit)
Rekisterikartta: VariPro-käyttöliittymän rekisterikartta v1.4 (N03104128)

Konfiguraatio: aseta ympäristömuuttujat tai muokkaa alla olevia oletuksia.
  VARIPRO_MODBUS_HOST   Modbus TCP -osoite (oletus 192.168.1.100)
  VARIPRO_MODBUS_PORT   portti (oletus 502)
  VARIPRO_MQTT_HOST     MQTT-brokerin osoite (oletus 192.168.1.10)
  VARIPRO_MQTT_PORT     portti (oletus 1883)
  VARIPRO_MQTT_USER     käyttäjä (valinnainen)
  VARIPRO_MQTT_PASS     salasana (valinnainen)
  VARIPRO_SLAVE         Modbus slave -osoite (oletus 1)

Lisenssi: MIT. Kehitetty iteratiivisesti Claude-tekoälyn avustuksella.
"""

import os
import time
import json
import logging
from datetime import datetime, timedelta
from pymodbus.client import ModbusTcpClient
import paho.mqtt.client as mqtt


class OptimizedVariProBridge:
    def __init__(self):
        self.logger = logging.getLogger(__name__)

        # Konfiguraatio (ympäristömuuttujat tai oletukset)
        self.modbus_host = os.environ.get('VARIPRO_MODBUS_HOST', '192.168.1.100')
        self.modbus_port = int(os.environ.get('VARIPRO_MODBUS_PORT', '502'))
        self.mqtt_host = os.environ.get('VARIPRO_MQTT_HOST', '192.168.1.10')
        self.mqtt_port = int(os.environ.get('VARIPRO_MQTT_PORT', '1883'))
        self.mqtt_user = os.environ.get('VARIPRO_MQTT_USER', '')
        self.mqtt_pass = os.environ.get('VARIPRO_MQTT_PASS', '')
        self.slave = int(os.environ.get('VARIPRO_SLAVE', '1'))

        # Optimointi
        self.min_poll_delay = 0.2  # 200ms ryhmien välillä (manuaalin vaatimus)
        self.cache = {}
        self.last_poll = {}

        # Kevyt kirjoitussuojaus - vain äskettäin kirjoitetut rekisterit
        self.recent_writes = {}  # {rekisteri: (arvo, aika)}

        # Fan only -tila
        self.fan_only_active = False
        self.fanonly_snapshot = None
        self.fanonly_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'varipro_fanonly.json')
        self.FANONLY_DEADZONE_RAW = 150  # 15.0 °C
        self.last_fanonly_rewrite = datetime.min

        # Rekisteriryhmät - lue peräkkäiset kerralla
        self.register_groups = [
            {
                'name': 'holding_main',
                'type': 'holding',
                'start': 0,
                'count': 5,  # 0-4: sovellustila, -, fan-komento, target home, target away
                'interval': 10,
                'registers': [
                    (0, 'mode', self.map_mode),
                    (1, None, None),  # Skip (ei määritelty)
                    (2, 'fan', self.map_fan),
                    (3, 'temp/target', lambda x: round(x * 0.1, 1)),      # 4x00004 Home target
                    (4, 'temp/target_away', lambda x: round(x * 0.1, 1))  # 4x00005 Away target
                ]
            },
            {
                'name': 'holding_deadzone',
                'type': 'holding',
                'start': 201,  # 4x00202 Poissa-tilan kuollut alue (skaalaus x0.1 varmistettu)
                'count': 1,
                'interval': 60,
                'registers': [
                    (201, 'deadzone', lambda x: round(x * 0.1, 1))
                ]
            },
            {
                'name': 'input_main',
                'type': 'input',
                'start': 3,
                'count': 8,  # 3-10: sovellustila ... lämmitys-PID
                'interval': 10,
                'registers': [
                    (3, 'app_state', lambda x: x),                            # 3x00004 Sovellustila
                    (4, None, None),                                          # Skip (3x00005 ei määritelty)
                    (5, 'fan_actual', lambda x: x),                           # 3x00006 Nykyinen puhaltimen säätö
                    (6, 'temp/current', lambda x: round(x * 0.1, 1)),         # 3x00007 Huoneen lämpötila
                    (7, 'temp/setpoint_active', lambda x: round(x * 0.1, 1)), # 3x00008 Nykyinen asetusarvo
                    (8, 'fan_speed', lambda x: x),                            # 3x00009 Puhaltimen nopeus %
                    (9, 'cooling_pid', lambda x: x),                          # 3x00010 Jäähdytyksen PID-lähtö %
                    (10, 'heating_pid', lambda x: x)                          # 3x00011 Lämmityksen PID-lähtö %
                ]
            },
            {
                'name': 'input_water',
                'type': 'input',
                'start': 115,
                'count': 2,  # 3x00116-117
                'interval': 30,
                'registers': [
                    (115, 'water_in', lambda x: round(x * 0.1, 1)),
                    (116, 'water_out', lambda x: round(x * 0.1, 1))
                ]
            },
            {
                'name': 'alarm',
                'type': 'input',
                'start': 102,  # 3x00103 Hälytys "Laite 1" (bittikenttä)
                'count': 1,
                'interval': 60,
                'registers': [
                    (102, 'alarm', lambda x: x)
                ]
            }
        ]

        # Hälytysbitit (3x00103) - suomenkieliset nimet
        self.alarm_bits = [
            (0,  'kondenssi',          'Kondenssianturin hälytys'),
            (1,  'puhallin1_rpm',      'Puuttuva kierrosluku, puhallin 1'),
            (2,  'puhallin2_rpm',      'Puuttuva kierrosluku, puhallin 2'),
            (3,  'anturi_vesi_sisaan', 'Anturivika, vesi sisään'),
            (4,  'anturi_vesi_ulos',   'Anturivika, vesi ulos'),
            (11, 'parametritiedosto',  'Parametritiedosto'),
            (15, 'suodatin',           'Suodattimen puhdistus')
        ]

        # Modbus client
        self.modbus = ModbusTcpClient(
            self.modbus_host,
            port=self.modbus_port,
            timeout=5,
            retries=2,
            retry_on_empty=True
        )

        # MQTT client
        self.mqtt = mqtt.Client("varipro_optimized")
        if self.mqtt_user:
            self.mqtt.username_pw_set(self.mqtt_user, self.mqtt_pass)
        self.mqtt.on_connect = self.on_mqtt_connect
        self.mqtt.on_message = self.on_mqtt_message

    # ------------------------------------------------------------------
    # Mappaukset
    # ------------------------------------------------------------------

    def map_mode(self, value):
        """Muunna sovellustila HA hvac modeksi - off ja cool (fan_only hoidetaan erikseen)"""
        modes = {1: 'off', 2: 'cool', 3: 'cool'}  # 3 (poissa) on myös cool
        return modes.get(value, 'off')

    def determine_preset_from_register(self, mode_register_value):
        return 'away' if mode_register_value == 3 else 'home'

    def get_current_preset(self):
        """Nykyinen preset. Fan onlyssa palautetaan sentinel, jotta pollaus
        ei julkaise aktiivista tavoitetta Nonen päälle (debug-topicit
        target_home/target_away päivittyvät silti normaalisti)."""
        if self.fan_only_active:
            return 'fan_only'
        mode_register = self.cache.get('mode_register_raw', 2)
        return self.determine_preset_from_register(mode_register)

    def map_fan(self, value):
        # Lukusuunta: 0 käännetään "off"-tekstiksi jos laite sitä näyttää,
        # mutta sitä ei tarjota valittavaksi (ks. fan-komentojen käsittely).
        fans = {0: 'off', 1: 'low', 2: 'medium', 3: 'high', 4: 'auto'}
        return fans.get(value, 'auto')

    def map_fan_actual_text(self, value):
        texts = {0: 'Pois', 1: 'Matala', 2: 'Keskitaso', 3: 'Korkea', 4: 'Auto'}
        return texts.get(value, f'Tuntematon ({value})')

    def map_app_state_text(self, value):
        texts = {0: 'Alustus', 1: 'Valmiustila', 2: 'Normaali', 3: 'Poissa',
                 4: 'Huuhtelu', 5: 'Pysähtynyt', 10: 'Testitila'}
        return texts.get(value, f'Tuntematon ({value})')

    # ------------------------------------------------------------------
    # Action
    # ------------------------------------------------------------------

    def determine_action(self):
        """hvac_action jäähdytys-PID:n ja puhaltimen perusteella."""
        if not self.fan_only_active and self.cache.get('mode_register_raw', 2) == 1:
            return 'off'
        cooling_pid = self.cache.get('cooling_pid_value', 0)
        fan_speed = self.cache.get('fan_speed_value', 0)
        if cooling_pid > 0:
            return 'cooling'
        elif fan_speed > 4:
            return 'fan'
        else:
            return 'idle'

    def publish_action_if_changed(self):
        action = self.determine_action()
        if self.cache.get('current_action') != action:
            self.mqtt.publish("varipro/action", action, retain=True)
            self.cache['current_action'] = action
            self.logger.debug(
                f"Published action: {action} "
                f"(pid: {self.cache.get('cooling_pid_value', 0)}%, "
                f"fan: {self.cache.get('fan_speed_value', 0)}%)"
            )

    # ------------------------------------------------------------------
    # Hälytykset
    # ------------------------------------------------------------------

    def publish_alarm_bits(self, raw_value):
        for bit, key, _name in self.alarm_bits:
            state = 'ON' if raw_value & (1 << bit) else 'OFF'
            cache_key = f'alarm_bit_{key}'
            if self.cache.get(cache_key) != state:
                self.mqtt.publish(f"varipro/alarm/{key}", state, retain=True)
                self.cache[cache_key] = state
                if state == 'ON':
                    self.logger.warning(f"Hälytys aktiivinen: {key}")

    # ------------------------------------------------------------------
    # Kirjoitussuojaus
    # ------------------------------------------------------------------

    def should_skip_update(self, register_addr, new_value):
        if register_addr not in self.recent_writes:
            return False
        written_value, write_time = self.recent_writes[register_addr]
        if (datetime.now() - write_time).total_seconds() < 2.0:
            if new_value == written_value:
                return True
        del self.recent_writes[register_addr]
        return False

    def mark_recent_write(self, register_addr, value):
        self.recent_writes[register_addr] = (value, datetime.now())

    # ------------------------------------------------------------------
    # Fan only -tilannekuva
    # ------------------------------------------------------------------

    def save_fanonly_snapshot(self, snapshot):
        try:
            with open(self.fanonly_file, 'w') as f:
                json.dump(snapshot, f)
        except Exception as e:
            self.logger.error(f"Fan only -tilannekuvan tallennus epäonnistui: {e}")

    def load_fanonly_snapshot(self):
        try:
            if os.path.exists(self.fanonly_file):
                with open(self.fanonly_file) as f:
                    return json.load(f)
        except Exception as e:
            self.logger.error(f"Fan only -tilannekuvan luku epäonnistui: {e}")
        return None

    def clear_fanonly_snapshot(self):
        try:
            if os.path.exists(self.fanonly_file):
                os.remove(self.fanonly_file)
        except Exception as e:
            self.logger.error(f"Fan only -tilannekuvan poisto epäonnistui: {e}")

    def enter_fan_only(self):
        """Siirry fan only -tilaan: away + kuollut alue 15 °C."""
        snapshot = {
            'preset': self.get_current_preset(),
            'deadzone_raw': self.cache.get('deadzone_raw', 20),
            'fan': self.cache.get('holding_main_2', 'auto'),
            'time': datetime.now().isoformat()
        }
        self.save_fanonly_snapshot(snapshot)
        self.fanonly_snapshot = snapshot

        self.modbus.write_register(201, self.FANONLY_DEADZONE_RAW, slave=self.slave)
        self.mark_recent_write(201, round(self.FANONLY_DEADZONE_RAW * 0.1, 1))
        self.modbus.write_register(0, 3, slave=self.slave)
        self.cache['mode_register_raw'] = 3

        # Puhallin manuaalille jos auto/off (auto pysäyttäisi puhaltimen ilman jäähdytystarvetta)
        if snapshot['fan'] in ('auto', 'off', None):
            self.modbus.write_register(2, 2, slave=self.slave)
            self.mark_recent_write(2, 'medium')
            self.cache['holding_main_2'] = 'medium'
            self.mqtt.publish("varipro/fan", "medium", retain=True)

        self.fan_only_active = True
        self.last_fanonly_rewrite = datetime.now()

        self.mqtt.publish("varipro/mode", "fan_only", retain=True)
        self.cache['holding_main_0'] = 'fan_only'
        self.mqtt.publish("varipro/deadzone",
                          str(round(self.FANONLY_DEADZONE_RAW * 0.1, 1)), retain=True)
        # Fan onlyssa ei ole tavoitelämpöä - None piilottaa kortin lämpötilasäätimen
        self.mqtt.publish("varipro/temp/target", "None", retain=True)
        self.logger.info(f"Fan only PÄÄLLE (tilannekuva: {snapshot})")

    def exit_fan_only(self, write_app_state=True):
        """Poistu fan only -tilasta ja palauta tilannekuva.
        write_app_state=False jos tila muuttui jo muualta (paneeli) tai
        kutsuja kirjoittaa sovellustilan itse (mode/preset-komento)."""
        snapshot = self.fanonly_snapshot or self.load_fanonly_snapshot() or {}
        deadzone_raw = snapshot.get('deadzone_raw', 20)
        preset = snapshot.get('preset', 'home')

        try:
            self.modbus.write_register(201, deadzone_raw, slave=self.slave)
            self.mark_recent_write(201, round(deadzone_raw * 0.1, 1))
            if write_app_state:
                reg = 3 if preset == 'away' else 2
                self.modbus.write_register(0, reg, slave=self.slave)
                self.cache['mode_register_raw'] = reg
        except Exception as e:
            self.logger.error(f"Fan only -palautus epäonnistui: {e}")

        self.fan_only_active = False
        self.fanonly_snapshot = None
        self.clear_fanonly_snapshot()

        # Palauta puhaltimen tila tilannekuvasta (fan only on "kupla":
        # myös enter-vaiheen medium-pakotus ja käyttäjän fan onlyn aikana
        # tekemät vaihdot perutaan)
        orig_fan = snapshot.get('fan')
        if orig_fan and self.cache.get('holding_main_2') != orig_fan:
            fans = {'off': 0, 'low': 1, 'medium': 2, 'high': 3, 'auto': 4}
            try:
                self.modbus.write_register(2, fans.get(orig_fan, 4), slave=self.slave)
                self.mark_recent_write(2, orig_fan)
                self.cache['holding_main_2'] = orig_fan
                self.mqtt.publish("varipro/fan", orig_fan, retain=True)
                self.logger.info(f"Puhallin palautettu: {orig_fan}")
            except Exception as e:
                self.logger.error(f"Puhaltimen palautus epäonnistui: {e}")

        self.cache['current_preset'] = preset
        self.mqtt.publish("varipro/preset", preset, retain=True)
        self.mqtt.publish("varipro/deadzone",
                          str(round(deadzone_raw * 0.1, 1)), retain=True)
        # Palauta tavoitelämpö kortille (None -> palautetun presetin arvo)
        target = self.cache.get('holding_main_4' if preset == 'away' else 'holding_main_3')
        if target is not None:
            self.mqtt.publish("varipro/temp/target", str(target), retain=True)
            self.cache['current_target_temp'] = target
        self.logger.info(f"Fan only POIS (palautettu preset: {preset}, kuollut alue raw: {deadzone_raw})")
        return preset

    def restore_fanonly_on_startup(self):
        """Jos edellinen ajo kaatui kesken fan onlyn, palauta normaalitila."""
        snap = self.load_fanonly_snapshot()
        if not snap:
            return
        self.logger.warning("Fan only -tilannekuva löytyi käynnistyksessä - palautetaan normaalitila")
        try:
            self.modbus.write_register(201, snap.get('deadzone_raw', 20), slave=self.slave)
            reg = 3 if snap.get('preset') == 'away' else 2
            self.modbus.write_register(0, reg, slave=self.slave)
        except Exception as e:
            self.logger.error(f"Käynnistyspalautus epäonnistui: {e}")
        self.clear_fanonly_snapshot()

    # ------------------------------------------------------------------
    # Pollaus
    # ------------------------------------------------------------------

    def should_poll_group(self, group):
        last = self.last_poll.get(group['name'], datetime.min)
        return datetime.now() - last >= timedelta(seconds=group['interval'])

    def read_group(self, group):
        if not self.should_poll_group(group):
            return

        try:
            if group['type'] == 'holding':
                result = self.modbus.read_holding_registers(
                    group['start'], group['count'], slave=self.slave)
            else:
                result = self.modbus.read_input_registers(
                    group['start'], group['count'], slave=self.slave)

            if result and not result.isError():
                preset_changed = False

                for i, reg_config in enumerate(group['registers']):
                    addr, topic, processor = reg_config

                    if topic is None:
                        continue

                    raw_value = result.registers[i]
                    processed_value = processor(raw_value) if processor else raw_value

                    # Mode-rekisteri: preset-määritys + fan only -valvonta
                    if addr == 0 and group['name'] == 'holding_main':
                        self.cache['mode_register_raw'] = raw_value

                        if self.fan_only_active:
                            if raw_value != 3:
                                # Joku muutti tilaa muualta (esim. paneelista)
                                self.logger.warning(
                                    "Fan only keskeytetty ulkopuolisella tilamuutoksella "
                                    f"(rekisteri 0 = {raw_value})")
                                self.exit_fan_only(write_app_state=False)
                                # processed_value pysyy map_moden mukaisena
                            else:
                                # Fan only voimassa: mode pysyy fan_only, preset jäädytetty
                                processed_value = 'fan_only'

                        if not self.fan_only_active:
                            old_preset = self.cache.get('current_preset', 'home')
                            new_preset = self.determine_preset_from_register(raw_value)
                            if old_preset != new_preset:
                                self.mqtt.publish("varipro/preset", new_preset, retain=True)
                                self.cache['current_preset'] = new_preset
                                self.logger.debug(f"Updated preset to: {new_preset} (mode register: {raw_value})")
                                preset_changed = True

                    # --- Target temp -erikoiskäsittelyt ---
                    if topic == 'temp/target':
                        current_preset = self.get_current_preset()
                        cache_key = f"{group['name']}_{addr}"
                        if self.cache.get(cache_key) != processed_value:
                            self.mqtt.publish("varipro/temp/target_home", str(processed_value), retain=True)
                        self.cache[cache_key] = processed_value
                        if current_preset == 'home':
                            if not self.should_skip_update(addr, processed_value):
                                self.mqtt.publish("varipro/temp/target", str(processed_value), retain=True)
                                self.cache['current_target_temp'] = processed_value
                        continue

                    elif topic == 'temp/target_away':
                        current_preset = self.get_current_preset()
                        cache_key = f"{group['name']}_{addr}"
                        if self.cache.get(cache_key) != processed_value:
                            self.mqtt.publish("varipro/temp/target_away", str(processed_value), retain=True)
                        self.cache[cache_key] = processed_value
                        if current_preset == 'away':
                            if not self.should_skip_update(addr, processed_value):
                                self.mqtt.publish("varipro/temp/target", str(processed_value), retain=True)
                                self.cache['current_target_temp'] = processed_value
                        continue

                    # --- Kuollut alue ---
                    if topic == 'deadzone':
                        if self.cache.get('deadzone_raw') != raw_value:
                            self.logger.info(f"Kuollut alue raaka-arvo: {raw_value} -> {processed_value} °C")
                        # Fan onlyn aikana emme tallenna 15.0-arvoa "oikeaksi" arvoksi
                        if not self.fan_only_active:
                            self.cache['deadzone_raw'] = raw_value

                    # --- Tekstisensorit ---
                    if topic == 'app_state':
                        text = self.map_app_state_text(raw_value)
                        if self.cache.get('app_state_text') != text:
                            self.mqtt.publish("varipro/app_state_text", text, retain=True)
                            self.cache['app_state_text'] = text

                    if topic == 'fan_actual':
                        text = self.map_fan_actual_text(raw_value)
                        if self.cache.get('fan_actual_text') != text:
                            self.mqtt.publish("varipro/fan_actual_text", text, retain=True)
                            self.cache['fan_actual_text'] = text

                    # --- Action-laskennan lähtöarvot ---
                    if topic == 'cooling_pid':
                        self.cache['cooling_pid_value'] = processed_value
                    if topic == 'fan_speed':
                        self.cache['fan_speed_value'] = processed_value
                    if topic == 'temp/current':
                        self.cache['current_room_temp'] = processed_value

                    # --- Hälytysbittien purku ---
                    if topic == 'alarm':
                        self.publish_alarm_bits(raw_value)

                    if self.should_skip_update(addr, processed_value):
                        self.logger.debug(f"Skipping {topic} update (recently written same value)")
                        continue

                    cache_key = f"{group['name']}_{addr}"
                    if self.cache.get(cache_key) != processed_value:
                        self.mqtt.publish(f"varipro/{topic}", str(processed_value), retain=True)
                        self.cache[cache_key] = processed_value
                        self.logger.debug(f"Published {topic}: {processed_value}")

                if group['name'] == 'input_main':
                    self.publish_action_if_changed()

                if preset_changed:
                    current_preset = self.get_current_preset()
                    if current_preset == 'away':
                        away_temp = self.cache.get('holding_main_4')
                        if away_temp is not None:
                            self.mqtt.publish("varipro/temp/target", str(away_temp), retain=True)
                            self.cache['current_target_temp'] = away_temp
                    else:
                        home_temp = self.cache.get('holding_main_3')
                        if home_temp is not None:
                            self.mqtt.publish("varipro/temp/target", str(home_temp), retain=True)
                            self.cache['current_target_temp'] = home_temp

                self.last_poll[group['name']] = datetime.now()

        except Exception as e:
            self.logger.error(f"Error reading {group['name']}: {e}")

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def publish_discovery(self):
        device = {
            "identifiers": ["varipro_001"],
            "name": "VariPro Chiller",
            "model": "Room Controller",
            "manufacturer": "Chiller",
            "via_device": "Modbus Bridge"
        }

        climate = {
            "name": "Ilmastointi",
            "unique_id": "varipro_chiller",
            "device": device,
            "current_temperature_topic": "varipro/temp/current",
            "temperature_state_topic": "varipro/temp/target",
            "temperature_command_topic": "varipro/temp/set",
            "mode_state_topic": "varipro/mode",
            "mode_command_topic": "varipro/mode/set",
            "modes": ["off", "cool", "fan_only"],
            "action_topic": "varipro/action",
            "fan_mode_state_topic": "varipro/fan",
            "fan_mode_command_topic": "varipro/fan/set",
            "fan_modes": ["auto", "low", "medium", "high"],
            "preset_mode_state_topic": "varipro/preset",
            "preset_mode_command_topic": "varipro/preset/set",
            "preset_modes": ["home", "away"],
            "temp_step": 0.5,
            "min_temp": 18,
            "max_temp": 24,
            "availability": [
                {
                    "topic": "varipro/bridge/status",
                    "payload_available": "online",
                    "payload_not_available": "offline"
                }
            ]
        }
        self.mqtt.publish("homeassistant/climate/varipro/config",
                          json.dumps(climate), retain=True, qos=1)

        # (topic, nimi, yksikkö, device_class)
        sensors = [
            ("fan_speed", "Puhaltimen nopeus", "%", None),
            ("cooling_pid", "Jäähdytysteho (PID)", "%", None),
            ("heating_pid", "Lämmitysteho (PID)", "%", None),
            ("water_in", "Tuloveden lämpötila", "°C", "temperature"),
            ("water_out", "Poistoveden lämpötila", "°C", "temperature"),
            ("temp/current", "Huonelämpötila", "°C", "temperature"),
            ("temp/setpoint_active", "Aktiivinen asetusarvo (laite)", "°C", "temperature"),
            ("temp/target_home", "Tavoitelämpötila, kotona", "°C", "temperature"),
            ("temp/target_away", "Tavoitelämpötila, poissa", "°C", "temperature"),
            ("app_state_text", "Sovellustila", None, None),
            ("fan_actual_text", "Puhaltimen tila (toteutunut)", None, None),
            ("alarm", "Hälytys (raaka)", None, None)
        ]
        for topic, name, unit, dev_class in sensors:
            uid = f"varipro_{topic.replace('/', '_')}"
            config = {
                "name": f"VariPro {name}",
                "unique_id": uid,
                "state_topic": f"varipro/{topic}",
                "device": device
            }
            if unit:
                config["unit_of_measurement"] = unit
                config["state_class"] = "measurement"  # HA:n pitkäaikaistilastot
            if dev_class:
                config["device_class"] = dev_class
            self.mqtt.publish(f"homeassistant/sensor/{uid}/config",
                              json.dumps(config), retain=True, qos=1)

        for _bit, key, name in self.alarm_bits:
            uid = f"varipro_halytys_{key}"
            config = {
                "name": f"VariPro {name}",
                "unique_id": uid,
                "state_topic": f"varipro/alarm/{key}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "problem",
                "device": device
            }
            self.mqtt.publish(f"homeassistant/binary_sensor/{uid}/config",
                              json.dumps(config), retain=True, qos=1)

        deadzone = {
            "name": "VariPro Poissa-tilan kuollut alue",
            "unique_id": "varipro_deadzone",
            "state_topic": "varipro/deadzone",
            "command_topic": "varipro/deadzone/set",
            "min": 0,
            "max": 15,
            "step": 0.5,
            "unit_of_measurement": "°C",
            "icon": "mdi:thermometer-lines",
            "device": device
        }
        self.mqtt.publish("homeassistant/number/varipro_deadzone/config",
                          json.dumps(deadzone), retain=True, qos=1)

        self.mqtt.publish("varipro/bridge/status", "online", retain=True)

    # ------------------------------------------------------------------
    # Pääsilmukka
    # ------------------------------------------------------------------

    def start(self):
        self.mqtt.connect(self.mqtt_host, self.mqtt_port)
        self.mqtt.loop_start()

        if not self.modbus.connect():
            self.logger.error("Modbus connection failed!")
            return

        # Palauta normaalitila, jos edellinen ajo kaatui kesken fan onlyn
        self.restore_fanonly_on_startup()

        self.publish_discovery()

        while True:
            try:
                for group in self.register_groups:
                    self.read_group(group)
                    time.sleep(self.min_poll_delay)

                # Fan only -vahtikoira: kirjoita tila uudelleen 5 min välein
                # (manuaali suosittelee rekisterien päivityskirjoitusta muutenkin)
                if self.fan_only_active and \
                   (datetime.now() - self.last_fanonly_rewrite).total_seconds() > 300:
                    try:
                        self.modbus.write_register(0, 3, slave=self.slave)
                        self.modbus.write_register(201, self.FANONLY_DEADZONE_RAW, slave=self.slave)
                        self.last_fanonly_rewrite = datetime.now()
                        self.logger.debug("Fan only -vahtikoira: tila kirjoitettu uudelleen")
                    except Exception as e:
                        self.logger.error(f"Fan only -vahtikoira epäonnistui: {e}")

                time.sleep(1)
            except KeyboardInterrupt:
                break
            except Exception as e:
                self.logger.error(f"Main loop error: {e}")
                time.sleep(5)

        # Siisti lopetus: jos fan only päällä, palauta normaalitila
        if self.fan_only_active:
            self.exit_fan_only(write_app_state=True)

        self.mqtt.publish("varipro/bridge/status", "offline", retain=True)
        self.mqtt.disconnect()
        self.modbus.close()

    # ------------------------------------------------------------------
    # MQTT-komennot
    # ------------------------------------------------------------------

    def on_mqtt_connect(self, client, userdata, flags, rc):
        client.subscribe("varipro/+/set")
        self.logger.info("MQTT connected")

    def on_mqtt_message(self, client, userdata, msg):
        try:
            topic = msg.topic.replace("varipro/", "").replace("/set", "")
            payload = msg.payload.decode()

            if topic in ("mode", "fan"):
                self.mqtt.publish(f"varipro/{topic}", payload, retain=True)

            time.sleep(0.3)

            if topic == "mode":
                if payload == 'fan_only':
                    if not self.fan_only_active:
                        self.enter_fan_only()
                    return

                # cool tai off - poistu ensin fan onlysta jos päällä
                restored_preset = None
                if self.fan_only_active:
                    restored_preset = self.exit_fan_only(write_app_state=False)

                if payload == 'off':
                    value = 1
                elif payload == 'cool' and restored_preset == 'away':
                    value = 3  # palataan away-viilennykseen josta fan only aloitettiin
                else:
                    value = 2

                self.modbus.write_register(0, value, slave=self.slave)
                self.mark_recent_write(0, payload)
                self.cache['holding_main_0'] = payload
                self.cache['mode_register_raw'] = value
                self.logger.debug(f"Set mode to {payload} (register {value})")

            elif topic == "fan":
                # 'off' poistettu fan-valikosta: laite ei pidä fan-rekisterin
                # arvoa 0 (palauttaa auton), ja se olisi joka tapauksessa sama
                # kuin koko laite off. Ohitetaan varmuuden vuoksi jos joku
                # lähettää sen (esim. vanha retained-viesti).
                if payload == 'off':
                    self.logger.info("Fan 'off' ei tuettu - ohitetaan (käytä mode off)")
                    current = self.cache.get('holding_main_2', 'auto')
                    self.mqtt.publish("varipro/fan", str(current), retain=True)
                    return
                fans = {'low': 1, 'medium': 2, 'high': 3, 'auto': 4}
                value = fans.get(payload, 4)
                if self.fan_only_active and payload == 'auto':
                    self.logger.info("Fan auto ei ole mielekäs fan only -tilassa - ohitetaan")
                    current = self.cache.get('holding_main_2', 'medium')
                    self.mqtt.publish("varipro/fan", str(current), retain=True)
                    return
                self.modbus.write_register(2, value, slave=self.slave)
                self.mark_recent_write(2, payload)
                self.cache['holding_main_2'] = payload
                self.logger.debug(f"Set fan to {payload} (register {value})")

            elif topic == "temp":
                if self.fan_only_active:
                    # Fan onlyssa ei ole tavoitetta - pidä None voimassa
                    self.logger.info("Lämpötilakomento ohitettu fan only -tilassa")
                    self.mqtt.publish("varipro/temp/target", "None", retain=True)
                    return

                current_preset = self.get_current_preset()
                register = 4 if current_preset == 'away' else 3
                value = int(float(payload) * 10)
                self.modbus.write_register(register, value, slave=self.slave)
                self.mark_recent_write(register, float(payload))
                self.cache[f"holding_main_{register}"] = float(payload)
                self.cache['current_target_temp'] = float(payload)
                self.mqtt.publish("varipro/temp/target", payload, retain=True)
                if current_preset == 'away':
                    self.mqtt.publish("varipro/temp/target_away", payload, retain=True)
                else:
                    self.mqtt.publish("varipro/temp/target_home", payload, retain=True)
                self.logger.debug(f"Set temp to {payload}°C on register {register} ({current_preset})")

            elif topic == "preset":
                # Preset-valinta fan onlyn aikana = poistuminen cool-tilaan
                if self.fan_only_active:
                    self.exit_fan_only(write_app_state=False)
                    self.mqtt.publish("varipro/mode", "cool", retain=True)
                    self.cache['holding_main_0'] = 'cool'

                # HA lisää preset-valikkoon aina automaattisesti "none"-vaihtoehdon.
                # Tulkitaan se samaksi kuin home, ja julkaistaan "home" takaisin
                # jotta UI ei jää näyttämään "Ei mitään".
                if payload == 'away':
                    effective_preset = 'away'
                    mode_register_value = 3
                else:  # home, none tai muu
                    effective_preset = 'home'
                    mode_register_value = 2

                if self.cache.get('mode_register_raw', 2) == 1:
                    self.logger.warning(f"Preset {effective_preset} requested while device is off - device will turn on")

                self.modbus.write_register(0, mode_register_value, slave=self.slave)

                current_ha_mode = self.cache.get('holding_main_0', 'cool')
                self.mark_recent_write(0, current_ha_mode)
                self.cache['mode_register_raw'] = mode_register_value
                self.cache['current_preset'] = effective_preset
                self.mqtt.publish("varipro/preset", effective_preset, retain=True)

                if effective_preset == 'away':
                    away_temp = self.cache.get('holding_main_4')
                    if away_temp is not None:
                        self.mqtt.publish("varipro/temp/target", str(away_temp), retain=True)
                        self.cache['current_target_temp'] = away_temp
                else:
                    home_temp = self.cache.get('holding_main_3')
                    if home_temp is not None:
                        self.mqtt.publish("varipro/temp/target", str(home_temp), retain=True)
                        self.cache['current_target_temp'] = home_temp

                self.logger.debug(f"Set preset to {effective_preset} (requested: {payload}), mode register to {mode_register_value}")

            elif topic == "deadzone":
                if self.fan_only_active:
                    # Kuollut alue on fan onlyn käytössä - älä anna sotkea
                    self.logger.info("Kuollut alue -komento ohitettu fan only -tilassa")
                    self.mqtt.publish("varipro/deadzone",
                                      str(round(self.FANONLY_DEADZONE_RAW * 0.1, 1)), retain=True)
                    return
                value = int(float(payload) * 10)
                self.modbus.write_register(201, value, slave=self.slave)
                self.mark_recent_write(201, float(payload))
                self.cache['deadzone_raw'] = value
                self.mqtt.publish("varipro/deadzone", payload, retain=True)
                self.logger.info(f"Set deadzone to {payload}°C (register 201 = {value})")

        except Exception as e:
            self.logger.error(f"Command error: {e}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    bridge = OptimizedVariProBridge()
    bridge.start()
