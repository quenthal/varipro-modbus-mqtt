# VariPro Modbus-MQTT Bridge

Python-silta, joka liittää **Chiller VariPro** -huonesäätimen (ja sen ohjaamat
GRAND / BOX / STUDIO VariPro -puhallinkonvektorit) Home Assistantiin yhtenä
MQTT-discovery-climate-entiteettinä.

VariPron natiivi Home Assistant -integrointi `modbus`-komponentin `climate`-alustalla
on mahdollinen, mutta se ei tue laitteen **poissa-tilaa** (oma tavoitelämpötila +
laajennettu kuollut alue) eikä tuota kunnollista toiminta- ja hälytystietoa. Tämä
silta hoitaa koko laitteen yhtenä siistinä entiteettinä ja lisää joukon
diagnostiikkasensoreita.

> Kehitetty iteratiivisesti Claude-tekoälyn avustuksella, oikealla laitteella testaten.

## Ominaisuudet

- **Yksi climate-entiteetti** koko laitteelle: hvac mode, puhallin, tavoitelämpö, toiminta
- **Preset home / away** — erilliset tavoitelämpötilarekisterit säilyvät toisistaan riippumatta
- **hvac_action jäähdytyksen PID-lähdöstä** — kortti näyttää rehellisesti jäähdyttääkö laite (`cooling`), pyöriikö puhallin ilman jäähdytystä (`fan`) vai onko se joutilaana (`idle`)
- **Fan only -tila** — synteettinen tuuletustila (away + kuollut alue 15 °C) joka ei riko oikeaa away-tavoitetta; tilannekuva tallennetaan levylle, joten uudelleenkäynnistys palautuu siististi
- **Hälytykset binary-sensoreina** — kondenssi, puhallinvika, anturiviat, suodatinmuistutus (purettu hälytysbittikentästä)
- **Diagnostiikkasensorit** — jäähdytys-/lämmitys-PID, vesien tulo/lähtö, aktiivinen asetusarvo, puhaltimen toteutunut tila, huonelämpötila
- **Poissa-tilan kuollut alue** säädettävänä number-entiteettinä
- **Optimoitu väyläkäyttö** — peräkkäiset rekisterit luetaan ryhmissä, porrastetut pollausvälit, välimuisti ja kevyt kirjoitussuojaus estävät UI:n välkkymisen

## Vaatimukset

- Python 3.9+
- `pymodbus`, `paho-mqtt`
- MQTT-broker (esim. Mosquitto) jonka Home Assistant näkee
- Home Assistantin MQTT-integraatio käytössä (discovery päällä)
- VariPro-säädin RS-485 → Modbus TCP -yhdyskäytävän takana (TCP/502)

## Asennus

```bash
# Riippuvuudet (Debian/Ubuntu/LXC)
sudo apt install python3-pymodbus python3-paho-mqtt
# tai:  pip3 install pymodbus paho-mqtt

git clone https://github.com/quenthal/varipro-modbus-mqtt.git
cd varipro-modbus-mqtt
```

Aseta yhteystiedot ympäristömuuttujilla (ks. `varipro.env.example`) tai muokkaa
oletuksia `varipro_bridge.py`:n alusta.

```bash
VARIPRO_MODBUS_HOST=192.168.1.215 VARIPRO_MQTT_HOST=192.168.1.10 \
  python3 varipro_bridge.py
```

### Systemd-palveluna

```bash
sudo cp varipro_bridge.py /opt/
sudo cp varipro.env.example /etc/varipro.env   # muokkaa arvot
sudo cp varipro.service /etc/systemd/system/
sudo systemctl enable --now varipro
sudo systemctl status varipro
```

## Konfiguraatio

Kaikki asetukset ympäristömuuttujina (oletus suluissa):

| Muuttuja | Kuvaus |
|---|---|
| `VARIPRO_MODBUS_HOST` | Modbus TCP -yhdyskäytävän osoite (`192.168.1.100`) |
| `VARIPRO_MODBUS_PORT` | Modbus-portti (`502`) |
| `VARIPRO_MQTT_HOST` | MQTT-brokerin osoite (`192.168.1.10`) |
| `VARIPRO_MQTT_PORT` | MQTT-portti (`1883`) |
| `VARIPRO_MQTT_USER` | MQTT-käyttäjä (tyhjä = ei autentikointia) |
| `VARIPRO_MQTT_PASS` | MQTT-salasana |
| `VARIPRO_SLAVE` | Modbus slave -osoite (`1`) |

## Modbus-rekisterikartta (käytetyt)

VariPro-käyttöliittymän rekisterikartta v1.4 (N03104128). Huomaa: osoitteet
ovat 0-pohjaisia (pymodbus), eli esim. "4x00001" = osoite 0.

| Osoite | Tyyppi | L/K | Sisältö | Skaalaus |
|---|---|---|---|---|
| 0 | holding | K | Sovellustila (1=off, 2=normaali, 3=poissa, 4=huuhtelu, 5=pysähtynyt, 10=testi) | — |
| 2 | holding | K | Puhallin (0=off, 1=low, 2=medium, 3=high, 4=auto) | — |
| 3 | holding | K | Tavoitelämpö, kotona | ×0.1 °C |
| 4 | holding | K | Tavoitelämpö, poissa | ×0.1 °C |
| 201 | holding | K | Poissa-tilan kuollut alue (0–15 °C) | ×0.1 °C |
| 3 | input | L | Sovellustila (nykyinen) | — |
| 5 | input | L | Puhaltimen säätö (toteutunut) | — |
| 6 | input | L | Huonelämpötila | ×0.1 °C |
| 7 | input | L | Aktiivinen asetusarvo | ×0.1 °C |
| 8 | input | L | Puhaltimen nopeus | % |
| 9 | input | L | Jäähdytyksen PID-lähtö | % |
| 10 | input | L | Lämmityksen PID-lähtö | % |
| 102 | input | L | Hälytys (bittikenttä, ks. alla) | — |
| 115–116 | input | L | Tulo-/poistoveden lämpötila | ×0.1 °C |

**Hälytysbitit (rekisteri 102):** 0=kondenssi, 1=puhallin 1 rpm, 2=puhallin 2 rpm,
3=anturivika vesi sisään, 4=anturivika vesi ulos, 11=parametritiedosto,
15=suodatinmuistutus.

**Väylän vaatimukset (manuaali):** pollausviive ≥ 200 ms, timeout 2000 ms,
kirjoitukset ≥ 5 min välein. Silta noudattaa näitä.

## MQTT-topicit

| Topic | Suunta | Sisältö |
|---|---|---|
| `varipro/temp/current` | pub | Huonelämpötila |
| `varipro/temp/target` | pub | Aktiivisen presetin tavoite (`None` fan onlyssa) |
| `varipro/temp/target_home` / `_away` | pub | Presettien tavoitteet erikseen |
| `varipro/temp/set` | sub | Aseta tavoite |
| `varipro/mode` ⇄ `/set` | pub/sub | off / cool / fan_only |
| `varipro/fan` ⇄ `/set` | pub/sub | auto / low / medium / high |
| `varipro/preset` ⇄ `/set` | pub/sub | home / away |
| `varipro/action` | pub | idle / cooling / fan / off |
| `varipro/deadzone` ⇄ `/set` | pub/sub | Poissa-tilan kuollut alue |
| `varipro/alarm/<nimi>` | pub | Hälytysbitit (ON/OFF) |
| `varipro/bridge/status` | pub | online / offline (availability) |

## Suunnitteluvalintoja

- **mode vs. preset:** laitteen rekisteri 0 kantaa sekä päälle/pois- että
  home/away-tiedon. Silta erottaa nämä HA:n käsitteiksi: `hvac_mode` (off/cool)
  kertoo mitä laite tekee, `preset` (home/away) millä profiililla.
- **Fan only** ei ole laitteen natiivi tila. Se toteutetaan asettamalla poissa-tila
  + kuollut alue 15 °C (jäähdytys ei käynnisty) ja manuaalinen puhallinnopeus.
  Oikea away-tavoite ei muutu, ja tila palautetaan levylle tallennetusta
  tilannekuvasta — myös sillan kaatuessa.
- **Away-preset ei säily laitteen off-syklin yli** (tietoinen valinta): off→cool
  palaa home-tilaan. Jos tarve, hoida se HA-automaatiolla, ei sillan logiikalla.

## Rajoitukset

- Testattu yhdellä laitteella (1 konvektori / säädin). Useamman laitteen tuki
  vaatisi hälytys-/vesirekisterien laiteindeksoinnin (3x002xx jne.).
- Kuolleen alueen rekisteri (201) ei ole virallisessa rekisteritaulukossa, mutta
  on dokumentoitu tekstissä ja varmistettu toimivaksi tällä ohjelmistoversiolla
  (0.36). Muilla versioilla kannattaa varmistaa raaka-arvo ennen fan onlyn käyttöä.

## Lisenssi

MIT — ks. [LICENSE](LICENSE).

Ei yhteydessä Chiller Oy:hyn. VariPro on Chiller Oy:n tavaramerkki. Käytä omalla
vastuulla; luvaton parametrien muuttaminen voi vaikuttaa laitteen takuuseen.
