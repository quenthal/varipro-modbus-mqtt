# Muutokset

Versiot: `VUOSI.KUUKAUSI.N` (N = kuukauden julkaisun järjestysnumero). Edellinen versio oli 1.x.

## 2026.10.1

Luotettavuusjulkaisu. Topicit ja Home Assistantin `unique_id`:t ovat ennallaan;
päivitysohje: README → "Päivitys versiosta 1.x".

### Korjattu
- Toimii nykyisillä kirjastoilla (pymodbus 3.10+, paho-mqtt 2.x); v1 ei
  käynnistynyt niillä lainkaan. Riippuvuuksille on nyt versiorajat.
- `systemctl stop`/`restart` (SIGTERM) palauttaa fan only -tilan ja julkaisee
  offline-tilan; aiemmin vain Ctrl+C teki näin.
- MQTT last will: kaatunut silta näkyy HA:ssa "ei käytettävissä" -tilassa.
- Modbus-väylää käytetään vain yhdestä säikeestä (MQTT-komennot jonon kautta);
  aiemmin komennot ja pollaus saattoivat mennä väylällä ristiin.
- Kaatumisen jälkeinen palautus palauttaa myös puhaltimen, ei kumoa paneelista
  tehtyä tilamuutosta ja palauttaa off-tilan, jos fan only aloitettiin siitä.
- Kirjoitussuoja ohittaa nyt kirjoituksen jälkeen luetun vanhan arvon (aiemmin se
  ohitti vain saman arvon, joten UI saattoi hypätä takaisin).
- Fan only -siirtymän osittainen epäonnistuminen perutaan; epäonnistunut
  palautus yritetään uudelleen eikä 15 °C kuollutta aluetta tallenneta oikeaksi.
- Lämpötilat pyöristetään (21.7 → 217, ei 216). Komentojen arvoalueet
  tarkistetaan, ja virheellisiä mode/fan-arvoja ei kaiuteta tilaan.

### Lisätty
- Modbus-yhteyden availability (`varipro/modbus/status`).
- Discovery ja tilat julkaistaan uudelleen MQTT-uudelleenyhdistyessä ja HA:n
  käynnistyessä (`homeassistant/status`).
- `heating`-toimintatila lämmityksen PID-lähdöstä.
- Asetukset: `VARIPRO_TOPIC_PREFIX` (useampi säädin), `VARIPRO_DISCOVERY_PREFIX`,
  `VARIPRO_MIN_TEMP`/`MAX_TEMP`, `VARIPRO_DEADZONE_MAX`, `VARIPRO_MODBUS_TIMEOUT`,
  `VARIPRO_STATE_DIR`, `VARIPRO_LOG_LEVEL`.
- `pyproject.toml` ja `varipro-bridge`-komento; asennus omaan venviin.
- Testit (pytest) ja lint (ruff).

### Muutettu
- systemd-palvelu ajetaan ilman roottia (`DynamicUser`), tilannekuva
  hakemistossa `/var/lib/varipro/`, peruskovennukset.
- Diagnostiikkasensorit `entity_category: diagnostic`, kuollut alue `config`;
  nimistä poistui "VariPro"-etuliite; virheellinen `via_device` poistettu;
  `origin`-tieto lisätty.
- `VARIPRO_MODBUS_HOST` on pakollinen (ei enää esimerkkiosoitetta oletuksena),
  ja `VARIPRO_MQTT_HOST`:n oletus on `localhost`.
- Kyselyjen väli (≥ 200 ms) koskee nyt myös kirjoituksia.
- `cool`-valinta away-tilassa pitää away-presetin (aiemmin vaihtui homeksi).
- Fan onlyn 5 minuutin uudelleenkirjoitus poistettu: muuttumattomien arvojen
  kirjoittaminen on turhaa, ja muuttuneet arvot ovat käyttäjän paneelivalintoja.
