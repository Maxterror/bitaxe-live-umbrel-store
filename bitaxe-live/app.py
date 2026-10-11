import os, json, time, io, threading, ipaddress, logging
from pathlib import Path
from functools import wraps
from flask import Flask, request, redirect, render_template_string, jsonify, send_file, abort, session
import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT=Path('/app/data'); ROOT.mkdir(parents=True,exist_ok=True)
FILE=ROOT/'settings.json'
HISTORY_FILE=ROOT/'history.json'
GRAPH_FIELDS=('hashrate_ghs','device_power_w','asic_temp_c','vr_temp_c','fans','free_ram')
DEFAULT={'miners':[],'discord_token':'','discord_channel_id':'','discord_guild_id':'','admin_role_ids':'','title':'BITAXE LIVE','refresh_seconds':10,'temp_limits':{'asic_yellow':60,'asic_red':70,'vr_yellow':65,'vr_red':80},'theme':{'bg':'#0b1116','panel':'#17242a','text':'#ecf8f7','accent':'#46e0c2'},'fields':['hashrate_ghs','asic_temp_c','vr_temp_c','device_power_w','fans','accepted_shares','rejected_shares','best_share_all_time','uptime_seconds'],'selected':0,'block_alert':None,'block_baselines':{},'logo':False,'background_enabled':False,'panel_opacity':100,'graph_bg':'#17262b','graph_opacity':100,'background_dim':0,'background_fit':'cover','dashboard_message_id':'','font_family':'dejavu','font_title_size':43,'font_value_size':38,'font_label_size':22,'language':'de','setup_complete':False,'bot_enabled':False,'discord_bots':[]}
LOCK=threading.RLock(); app=Flask(__name__); app.secret_key=os.environ.get('APP_SECRET',os.urandom(32).hex())
logging.basicConfig(level=logging.INFO)

def config():
    with LOCK:
        try:
            d = json.loads(FILE.read_text())
            d = migrate_discord_bots(d)
            return {**DEFAULT,**d,'theme':{**DEFAULT['theme'],**d.get('theme',{})},'temp_limits':{**DEFAULT['temp_limits'],**d.get('temp_limits',{})}}
        except (FileNotFoundError,ValueError): return dict(DEFAULT)

def discord_bot_defaults():
    commands = {
        'dashboard': True,
        'miners': True,
        'start': True,
        'pause': True,
        'restart': True,
    }
    commands.update({
        f'miner{i}': True
        for i in range(1, 21)
    })
    return commands

def migrate_discord_bots(d):
    """Bestehenden Discord-Bot einmalig in die neue Struktur übernehmen."""
    if d.get('discord_bots_migrated', False):
        return d

    if d.get('discord_bots'):
        d['discord_bots_migrated'] = True
        return d

    if not d.get('discord_token'):
        return d

    d['discord_bots'] = [{
        'id': 'bot1',
        'name': 'Hauptbot',
        'token': d.get('discord_token', ''),
        'channel_id': d.get('discord_channel_id', ''),
        'guild_id': d.get('discord_guild_id', ''),
        'admin_role_ids': d.get('admin_role_ids', ''),
        'dashboard_message_id': d.get('dashboard_message_id', ''),
        'synced_guild_id': '',
        'enabled': bool(d.get('bot_enabled', False)),
        'commands_enabled': True,
    }]
    d['discord_bots_migrated'] = True
    return d


def find_discord_bot(d, bot_id):
    """Discord-Bot anhand seiner eindeutigen ID finden."""
    for bot in d.get('discord_bots', []):
        if isinstance(bot, dict) and bot.get('id') == bot_id:
            return bot
    return None

def discord_bot_name(bot):
    """Anzeigenamen eines Discord-Bots zurückgeben."""
    return str(bot.get('name', 'Discord-Bot'))[:50]


def create_discord_bot(d, name):
    """Neuen Discord-Bot mit eigenen Einstellungen anlegen."""
    import uuid

    bot = {
        'id': 'bot_' + uuid.uuid4().hex[:12],
        'name': str(name).strip()[:50] or 'Neuer Discord-Bot',
        'token': '',
        'channel_id': '',
        'guild_id': '',
        'admin_role_ids': '',
        'dashboard_message_id': '',
        'synced_guild_id': '',
        'enabled': False,
        'commands_enabled': True,
    }

    d.setdefault('discord_bots', []).append(bot)
    return bot


def current_discord_bot(bot_id):
    """Aktuelle Einstellungen eines bestimmten Discord-Bots abrufen."""
    d = config()
    return find_discord_bot(d, bot_id)



def save_discord_message_id(bot_id, token, channel_id, message_id):
    """Dashboard-Nachrichten-ID sicher einem bestimmten Bot zuordnen."""
    with LOCK:
        d = config()
        bot = find_discord_bot(d, bot_id)

        if bot is None:
            return False

        if not bot.get('enabled', False):
            return False

        if bot.get('token') != token:
            return False

        if str(bot.get('channel_id', '')) != str(channel_id):
            return False

        bot['dashboard_message_id'] = str(message_id)
        save(d)
        return True




def save_discord_sync_state(bot_id, token, guild_id, commands_enabled):
    """Zuletzt erfolgreich synchronisierten Discord-Zustand speichern."""
    with LOCK:
        d = config()
        bot = find_discord_bot(d, bot_id)

        if bot is None:
            return False

        if not bot.get('enabled', False):
            return False

        if bot.get('token', '') != token:
            return False

        if str(bot.get('guild_id', '')).strip() != str(guild_id).strip():
            return False

        if bool(bot.get('commands_enabled', True)) != bool(commands_enabled):
            return False

        bot['synced_guild_id'] = str(guild_id).strip()
        bot['synced_commands_enabled'] = bool(commands_enabled)
        save(d)
        return True



def update_discord_bot(bot_id, changes):
    """Bot-Einstellungen atomar lesen, ändern und speichern."""
    with LOCK:
        d = config()
        bot = find_discord_bot(d, bot_id)

        if bot is None:
            return False

        if not isinstance(changes, dict):
            raise ValueError('Ungültige Bot-Einstellungen')

        bot.update(changes)
        save(d)
        return True


def active_discord_bots():
    """Alle aktivierten Discord-Bots ermitteln."""
    d = config()
    bots = d.get('discord_bots', [])

    return [
        bot.copy()
        for bot in bots
        if isinstance(bot, dict)
        and bot.get('enabled', False)
        and bot.get('token')
    ]


def save(d):
    with LOCK:
        d = migrate_discord_bots(d)
        p=FILE.with_suffix('.tmp'); p.write_text(json.dumps(d,indent=2)); p.replace(FILE)

HISTORY_LOCK = threading.RLock()

def graph_number(value):
    try:
        if isinstance(value, bool):
            return None
        number = float(value)
        return number if -1e15 < number < 1e15 else None
    except (TypeError, ValueError, OverflowError):
        return None

def record_history(miners):
    now = int(time.time())
    cutoff = now - 86400
    with HISTORY_LOCK:
        try:
            history = json.loads(HISTORY_FILE.read_text())
            if not isinstance(history, dict):
                history = {}
        except (OSError, ValueError):
            history = {}

        changed = False

        for miner in miners:
            raw = miner.get('raw')
            if not isinstance(raw, dict):
                continue
            values = {}
            for key in GRAPH_FIELDS:
                if key == 'fans':
                    fans = raw.get('fans') or []
                    value = fans[0].get('percent') if isinstance(fans, list) and fans and isinstance(fans[0], dict) else None
                else:
                    value = raw.get(key)
                values[key] = graph_number(value)

            ip = miner['ip']
            points = history.get(ip, [])
            if not isinstance(points, list):
                points = []
            points = [point for point in points if isinstance(point, dict) and point.get('time', 0) >= cutoff]
            old_points = history.get(ip, [])
            if not points or now - points[-1].get('time', 0) >= 2:
                points.append({'time': now, 'values': values})
            points = points[-43200:]
            if points != old_points:
                history[ip] = points
                changed = True

        if not changed:
            return

        tmp = HISTORY_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(history, separators=(',', ':')))
        tmp.replace(HISTORY_FILE)

def miner_ip(s):
    ip=ipaddress.ip_address(s)
    if not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast: raise ValueError('Nur private LAN-IP-Adressen erlaubt')
    return str(ip)
def telemetry(ip):
    try:
        r=requests.get(f'http://{miner_ip(ip)}/api/telemetry',timeout=3); r.raise_for_status(); j=r.json()
        return j if isinstance(j,dict) else None
    except (requests.RequestException,ValueError): return None

def auth(f):
    @wraps(f)
    def wrapper(*a,**k):
        password=os.environ.get('DASHBOARD_PASSWORD','')
        if password and not session.get('ok'):
            if request.path=='/login': return f(*a,**k)
            return redirect('/login')
        return f(*a,**k)
    return wrapper
@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST' and os.environ.get('DASHBOARD_PASSWORD') and request.form.get('password')==os.environ['DASHBOARD_PASSWORD']:
        session['ok']=True; return redirect('/')
    return '<form method="post"><h2>Bitaxe Live Login</h2><input type="password" name="password" required><button>Login</button></form>'

FIELDS={'hashrate_ghs':'Hashrate','asic_temp_c':'ASIC Temperatur','vr_temp_c':'VR Temperatur','device_power_w':'Leistung (geschätzt)','fans':'Lüfter','accepted_shares':'Accepted','rejected_shares':'Rejected','best_share_all_time':'Best Share','uptime_seconds':'Uptime','pool_latency_ms':'Pool-Latenz','core_voltage_mv':'Core mV','input_voltage_v':'Input V','active_pll_mhz':'Frequenz MHz','firmware_version':'Firmware'}
def value_color(key, value, limits):
    colors = {
        'hashrate_ghs': '#86efac',
        'device_power_w': '#e7b66e',
        'best_share_all_time': '#e7b66e',
        'accepted_shares': '#86efac',
        'rejected_shares': '#fb7185',
    }
    if key in ('asic_temp_c', 'vr_temp_c'):
        try:
            temp = float(value)
            prefix = 'asic' if key == 'asic_temp_c' else 'vr'
            if temp >= limits[prefix + '_red']:
                return '#fb7185'
            if temp >= limits[prefix + '_yellow']:
                return '#fb923c'
            return '#86efac'
        except (TypeError, ValueError, KeyError):
            return '#a1b5b7'
    return colors.get(key, '#ecf8f7')

def formatted(k,t):
    v=t.get(k)
    if k=='fans':
        fans=v or []; return str(fans[0].get('percent','?'))+' %' if fans else '–'
    if v is None: return '–'
    if k=='best_share_all_time':
        try:
            n = float(v)
            for factor, suffix in ((1e12, 'T'), (1e9, 'G'), (1e6, 'M'), (1e3, 'K')):
                if abs(n) >= factor:
                    return f'{n/factor:.2f} {suffix}'.replace('.', ',')
            return str(int(n)) if n.is_integer() else str(n).replace('.', ',')
        except (ValueError, TypeError):
            return str(v)
    if k=='hashrate_ghs': return f'{float(v)/1000:.2f} TH/s'
    if k in ('asic_temp_c','vr_temp_c'): return f'{float(v):.1f} °C'
    if k=='device_power_w': return f'{float(v):.1f} W*'
    if k=='uptime_seconds': return f'{int(v)//3600}h {(int(v)%3600)//60}m'
    if k=='pool_latency_ms': return f'{v} ms'
    return str(v)
def scan():
    d=config(); result=[]; changed=False
    for m in d['miners']:
        t=telemetry(m['ip']); result.append({'name':m['name'],'ip':m['ip'],'online':bool(t),'values':{k:formatted(k,t) for k in d['fields']} if t else {},'raw':t,'colors':{k:value_color(k,t.get(k),d['temp_limits']) for k in d['fields']} if t else {}})
        if t:
            key=m['ip']; count=sum(int(p.get('blocks_mined') or 0) for p in t.get('pools',[]))
            previous=d['block_baselines'].get(key)
            if previous is None: d['block_baselines'][key]=count; changed=True
            elif count>previous:
                d['block_baselines'][key]=count; changed=True
                if not d['block_alert']: d['block_alert']={'name':m['name'],'ip':key,'time':int(time.time()),'count':count}
            elif count<previous: d['block_baselines'][key]=count; changed=True
    if changed:
        with LOCK:
            current=config(); current['block_baselines'].update(d['block_baselines'])
            if d['block_alert'] and not current.get('block_alert'): current['block_alert']=d['block_alert']
            save(current)
    return result

PAGE='''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Bitaxe Live</title><style>
:root{color-scheme:dark}body{background:#0b1116;color:#eaf6f5;font:15px system-ui;margin:0;padding:25px}main{max-width:1000px;margin:auto}h1{color:#46e0c2}section{background:#17242a;padding:20px;border-radius:14px;margin:16px 0}input,select{background:#0b1116;color:white;border:1px solid #526a70;padding:10px;border-radius:7px;max-width:100%}input[type=text],input[type=password]{min-width:190px}button{background:#46e0c2;border:0;padding:10px 15px;border-radius:7px;cursor:pointer;font-weight:bold}form{display:flex;flex-wrap:wrap;gap:9px;align-items:center;margin:12px 0}.danger{background:#ef7582}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:10px}.tile{background:#0b1116;border-radius:10px;padding:14px}.muted{color:#a1b5b7}.alert{border:2px solid #ffd166;color:#ffd166}.preview{width:100%;max-width:780px;border-radius:12px}label{display:inline-flex;gap:6px;align-items:center;margin:5px 10px 5px 0}small{color:#a1b5b7}</style></head><body><main>
<div style="display:flex;justify-content:space-between;align-items:center;gap:12px"><h1>BITAXE LIVE DASHBOARD</h1><a href="/settings" style="color:#46e0c2;font-size:19px">⚙ {{'Einstellungen' if cfg.language=='de' else 'Settings'}}</a></div><p class="muted">HarloOS Miner-Schwarm · Umbrel Beta</p>
{% if cfg.block_alert %}<section class="alert"><h1>🏆 BLOCK FOUND! {{cfg.block_alert.name}}</h1><p>Bleibt bis zum manuellen Reset sichtbar.</p><form method="post" action="/reset-block"><button class="danger">Blockmeldung zurücksetzen</button></form></section>{% endif %}
<section><h2>{{'Live-Miner' if cfg.language=='de' else 'Live miners'}}</h2><div id="live">Lade Live-Daten…</div></section>
<section id="graph-section">
<h2>📈 TERROR LIVE GRAPH</h2>
<div class="graph-controls">
<button type="button" data-minutes="5">5 Min.</button>
<button type="button" data-minutes="15">15 Min.</button>
<button type="button" data-minutes="60">1 Std.</button>
<button type="button" data-minutes="1440">24 Std.</button>
</div>
<div id="graph-legend" style="display:flex;flex-wrap:wrap;gap:12px;margin:14px 0">
<label><input type="checkbox" data-graph-metric="hashrate_ghs" checked> <span style="color:#86efac">● Hashrate</span></label>
<label><input type="checkbox" data-graph-metric="device_power_w" checked> <span style="color:#e7b66e">● Watt</span></label>
<label><input type="checkbox" data-graph-metric="asic_temp_c" checked> <span style="color:#38bdf8">● ASIC</span></label>
<label><input type="checkbox" data-graph-metric="vr_temp_c" checked> <span style="color:#facc15">● VR</span></label>
<label><input type="checkbox" data-graph-metric="fans" checked> <span style="color:#c084fc">● Lüfter</span></label>
<label><input type="checkbox" data-graph-metric="free_ram"> <span style="color:#fb7185">● RAM</span></label>
</div>
<p class="muted" id="graph-status">Warte auf Messwerte…</p>
<canvas id="live-graph" width="960" height="360" style="width:100%;max-width:100%;background:#0b1116;border-radius:12px"></canvas>
</section>
<section><h2>{{'Miner verwalten' if cfg.language=='de' else 'Manage miners'}}</h2>{% for m in cfg.miners %}<form method="post" action="/remove/{{loop.index0}}"><b>{{m.name}}</b> <span class="muted">{{m.ip}}</span><button class="danger">{{'Entfernen' if cfg.language=='de' else 'Remove'}}</button></form>{% endfor %}<form method="post" action="/add"><input name="name" placeholder="Miner-Name" maxlength="50" required><input name="ip" placeholder="192.168.178.24" required><button>{{'Miner hinzufügen' if cfg.language=='de' else 'Add miner'}}</button></form></section>
<section><h2>{{'Dashboard-Design' if cfg.language=='de' else 'Dashboard design'}}</h2><form method="post" action="/design"><input name="title" value="{{cfg.title}}" maxlength="70" placeholder="Titel"><label>Hintergrund <input type="color" name="bg" value="{{cfg.theme.bg}}"></label><label>Karten <input type="color" name="panel" value="{{cfg.theme.panel}}"></label><label>Schrift <input type="color" name="text" value="{{cfg.theme.text}}"></label><label>Akzent <input type="color" name="accent" value="{{cfg.theme.accent}}"></label><div style="width:100%"><b>Werte anzeigen:</b><br>{% for key,label in fields.items() %}<label><input type="checkbox" name="fields" value="{{key}}" {% if key in cfg.fields %}checked{% endif %}>{{label}}</label>{% endfor %}</div><div style="width:100%;display:flex;flex-wrap:wrap;gap:12px;align-items:center"><label>Schriftart <select name="font_family">{% for key,name in font_options.items() %}<option value="{{key}}" {% if cfg.font_family==key %}selected{% endif %}>{{name}}</option>{% endfor %}</select></label><label>Titelgröße <input type="range" name="font_title_size" min="24" max="66" value="{{cfg.font_title_size}}" oninput="this.nextElementSibling.textContent=this.value"><output>{{cfg.font_title_size}}</output></label><label>Messwertgröße <input type="range" name="font_value_size" min="20" max="58" value="{{cfg.font_value_size}}" oninput="this.nextElementSibling.textContent=this.value"><output>{{cfg.font_value_size}}</output></label><label>Beschriftungsgröße <input type="range" name="font_label_size" min="14" max="32" value="{{cfg.font_label_size}}" oninput="this.nextElementSibling.textContent=this.value"><output>{{cfg.font_label_size}}</output></label></div>
<div style="width:100%;padding:15px 0">
<b>🎨 Hintergrundbild & Transparenz</b>
<p class="muted">Passe die Darstellung deiner Discord-Grafik an.</p>

<label>Kartendeckkraft
<input type="range" name="panel_opacity" min="0" max="100"
value="{{cfg.panel_opacity}}"
oninput="this.nextElementSibling.textContent=this.value+' %'">
<output>{{cfg.panel_opacity}} %</output>
</label>

<label>Hintergrund-Abdunklung
<input type="range" name="background_dim" min="0" max="90"
value="{{cfg.background_dim}}"
oninput="this.nextElementSibling.textContent=this.value+' %'">
<output>{{cfg.background_dim}} %</output>
</label>

<label>Graph-Hintergrundfarbe
<input type="color" name="graph_bg" value="{{cfg.graph_bg}}">
</label>

<label>Gitternetzfarbe
<input type="color" name="graph_grid_color"
value="{{cfg.graph_grid_color|default('#34444b')}}">
</label>

<label>Graph-Deckkraft
<input type="range" name="graph_opacity" min="0" max="100"
value="{{cfg.graph_opacity}}"
oninput="this.nextElementSibling.textContent=this.value+' %'">
<output>{{cfg.graph_opacity}} %</output>
</label>

<label>Bildanpassung
<select name="background_fit">
<option value="cover" {% if cfg.background_fit=='cover' %}selected{% endif %}>Ausfüllen</option>
<option value="contain" {% if cfg.background_fit=='contain' %}selected{% endif %}>Einpassen</option>
</select>
</label>
</div>
<div style="width:100%">
<b>🌡️ Temperatur-Farben (°C)</b>
<p class="muted">Grün unter Orange, Orange bis Rot, Rot ab der roten Grenze.</p>
<label>ASIC Orange ab <input type="number" name="asic_yellow" min="0" max="120" value="{{cfg.temp_limits.asic_yellow}}"></label>
<label>ASIC Rot ab <input type="number" name="asic_red" min="0" max="120" value="{{cfg.temp_limits.asic_red}}"></label>
<label>VR Orange ab <input type="number" name="vr_yellow" min="0" max="120" value="{{cfg.temp_limits.vr_yellow}}"></label>
<label>VR Rot ab <input type="number" name="vr_red" min="0" max="120" value="{{cfg.temp_limits.vr_red}}"></label>
</div><label>Intervall (Sekunden) <input type="number" name="refresh" min="2" max="300" value="{{cfg.refresh_seconds}}"></label><button>{{'Design speichern' if cfg.language=='de' else 'Save design'}}</button></form><form action="/logo" method="post" enctype="multipart/form-data"><label>Eigenes Logo (PNG/JPG, 128×128 empfohlen) <input type="file" name="logo" accept="image/png,image/jpeg" required></label><button>{{'Logo hochladen' if cfg.language=='de' else 'Upload logo'}}</button></form><div style="width:100%;margin-top:18px">
<h3>🖼️ Eigenes Hintergrundbild</h3>
<p class="muted">PNG oder JPG, maximal 5 MB. Das Bild wird für die Discord-Grafik verwendet.</p>
<form method="post" action="/background-upload" enctype="multipart/form-data">
<label>Hintergrundbild auswählen
<input type="file" name="background" accept="image/png,image/jpeg" required>
</label>
<button>Hintergrundbild hochladen</button>
</form>
{% if cfg.background_enabled %}
<form method="post" action="/background-remove">
<button class="danger">Hintergrundbild entfernen</button>
</form>
{% endif %}
</div>
<p><img class="preview" src="/preview.png?t={{stamp}}" alt="Dashboard Vorschau"></p></section>

<section>
<h2>🤖 Discord-Bots verwalten</h2>
<p class="muted">Jeder Bot hat eigene Discord-Zugangsdaten, einen eigenen Kanal und unabhängige Schalter.</p>

{% for bot in cfg.discord_bots %}
<div style="border:1px solid #45616a;border-radius:12px;padding:16px;margin:15px 0">
<h3>{{bot.name}}</h3>

<p>
Status: {{'🟢 Aktiviert' if bot.enabled else '⚪ Gestoppt'}}
<br>
Slash-Befehle: {{'🟢 Aktiviert' if bot.commands_enabled else '🔴 Deaktiviert'}}
</p>

<form method="post" action="/discord-bots/toggle">
<input type="hidden" name="bot_id" value="{{bot.id}}">
<button>{{'Bot stoppen' if bot.enabled else 'Bot starten'}}</button>
</form>

<form method="post" action="/discord-bots/commands-toggle">
<input type="hidden" name="bot_id" value="{{bot.id}}">
<button>{{'Alle Befehle AUS' if bot.commands_enabled else 'Alle Befehle EIN'}}</button>
</form>

<form method="post" action="/discord-bots/edit">
<input type="hidden" name="bot_id" value="{{bot.id}}">
<label>Bot-Name <input name="name" value="{{bot.name}}" required></label>
<label>Bot-Token <input type="password" name="token" placeholder="Leer lassen = bisherigen Token behalten"></label>
<label>Discord Server-ID <input name="guild" value="{{bot.guild_id}}"></label>
<label>Discord Kanal-ID <input name="channel" value="{{bot.channel_id}}"></label>
<label>Admin-Rollen-IDs <input name="roles" value="{{bot.admin_role_ids}}"></label>
<button>Einstellungen speichern</button>
</form>
</div>
{% endfor %}

<h3>➕ Weiteren Discord-Bot hinzufügen</h3>
<form method="post" action="/discord-bots/add">
<label>Bot-Name <input name="name" placeholder="z. B. Zweiter Discord-Server" maxlength="50" required></label>
<button>Bot hinzufügen</button>
</form>

<p class="muted">Maximal 10 Bots. Die Befehle /dashboard, /miners, /miner1 bis /miner20, /start, /pause und /restart werden gemeinsam ein- oder ausgeschaltet. Admin-Berechtigungen bleiben erhalten.</p>
</section>

<p class="muted">* Leistung ist ein HarloOS-Schätzwert. Blockalarm basiert auf Änderungen des blocks_mined-Zählers. Beim ersten Abruf wird nur ein Basiswert gespeichert.</p></main><script>
const graphTempLimits={
    asicYellow:{{ cfg.temp_limits.asic_yellow|int }},
    asicRed:{{ cfg.temp_limits.asic_red|int }},
    vrYellow:{{ cfg.temp_limits.vr_yellow|int }},
    vrRed:{{ cfg.temp_limits.vr_red|int }}
};
let graphMinutes=5;
let graphBusy=false;
let graphData={};

document.querySelectorAll('[data-graph-metric]').forEach(box=>{
    box.addEventListener('change',()=>drawGraph(graphData));
});


const graphMetrics=[
    ['hashrate_ghs','#86efac'],
    ['device_power_w','#e7b66e'],
    ['asic_temp_c','#38bdf8'],
    ['vr_temp_c','#facc15'],
    ['fans','#c084fc'],
    ['free_ram','#fb7185']
];

function updateGraphLegend(data){
    const points=Object.values(data)
        .flat()
        .filter(p=>p && p.values)
        .sort((a,b)=>b.time-a.time);

    const units={
        hashrate_ghs:' GH/s',
        device_power_w:' W',
        asic_temp_c:' °C',
        vr_temp_c:' °C',
        fans:' %',
        free_ram:''
    };

    document.querySelectorAll('[data-graph-metric]').forEach(box=>{
        const metric=box.dataset.graphMetric;
        const latest=points.find(p=>
            typeof p.values[metric]==='number' &&
            Number.isFinite(p.values[metric])
        );
        const label=box.parentElement.querySelector('span');
        if(!label)return;

        const name=label.textContent.split(' · ')[0];
        const value=latest
            ? latest.values[metric].toLocaleString('de-DE',{
                maximumFractionDigits:2
              })+(units[metric]||'')
            : 'keine Daten';

        label.textContent=name+' · '+value;
    });
}

function graphValueColor(metric,value,defaultColor){
    if(metric!=='asic_temp_c' && metric!=='vr_temp_c'){
        return defaultColor;
    }
    const isAsic=metric==='asic_temp_c';
    const yellow=isAsic?graphTempLimits.asicYellow:graphTempLimits.vrYellow;
    const red=isAsic?graphTempLimits.asicRed:graphTempLimits.vrRed;
    if(value>=red)return '#fb7185';
    if(value>=yellow)return '#fb923c';
    return '#86efac';
}

function drawGraph(data){
    const canvas=document.getElementById('live-graph');
    const ctx=canvas.getContext('2d');
    const w=canvas.width,h=canvas.height;
    const left=45,right=w-20,top=20,bottom=h-35;

    ctx.clearRect(0,0,w,h);
    ctx.strokeStyle='#263641';
    ctx.lineWidth=1;

    for(let i=0;i<=4;i++){
        const y=top+(bottom-top)*i/4;
        ctx.beginPath();
        ctx.moveTo(left,y);
        ctx.lineTo(right,y);
        ctx.stroke();
    }

    const now=Date.now()/1000;
    const start=now-graphMinutes*60;

    ctx.fillStyle='#9ca3af';
    ctx.font='13px sans-serif';
    ctx.textAlign='center';

    for(let i=0;i<=4;i++){
        const timestamp=start+(now-start)*i/4;
        const date=new Date(timestamp*1000);
        const label=date.toLocaleTimeString('de-DE',{
            hour:'2-digit',
            minute:'2-digit'
        });
        const x=left+(right-left)*i/4;
        ctx.fillText(label,x,h-12);
    }

    const miners=Object.values(data);

    graphMetrics.forEach(([metric,color])=>{
        const toggle=document.querySelector('[data-graph-metric="'+metric+'"]');
        if(toggle && !toggle.checked)return;
        const points=miners.flat().filter(p=>
            p && p.values &&
            typeof p.values[metric]==='number' &&
            Number.isFinite(p.values[metric])
        );
        if(!points.length)return;

        const values=points.map(p=>p.values[metric]);
        let min=Infinity;
        let max=-Infinity;
        for(const value of values){
            if(value<min)min=value;
            if(value>max)max=value;
        }
        const span=max-min||1;

        ctx.lineWidth=2;
        miners.forEach(series=>{
            let previous=null;
            series.forEach(p=>{
                const value=p.values && p.values[metric];
                if(typeof value!=='number'||!Number.isFinite(value)){
                    previous=null;
                    return;
                }
                const x=left+(p.time-start)/(graphMinutes*60)*(right-left);
                const y=bottom-(value-min)/span*(bottom-top);
                if(x<left||x>right){
                    previous=null;
                    return;
                }
                if(previous){
                    ctx.beginPath();
                    ctx.strokeStyle=graphValueColor(metric,value,color);
                    ctx.moveTo(previous.x,previous.y);
                    ctx.lineTo(x,y);
                    ctx.stroke();
                }
                previous={x,y};
            });
        });
    });
}

async function updateGraph(){
    if(graphBusy)return;
    graphBusy=true;
    try{
        const response=await fetch('/api/history?minutes='+graphMinutes);
        if(!response.ok)throw new Error('HTTP '+response.status);
        const data=await response.json();graphData=data;updateGraphLegend(graphData);drawGraph(graphData);
        const count=Object.values(data).reduce((sum,points)=>sum+points.length,0);
        document.getElementById('graph-status').textContent=
            count+' Messpunkte · Zeitraum: '+graphMinutes+' Minuten';
    }catch(error){
        document.getElementById('graph-status').textContent='Graph-Daten nicht erreichbar';
    }finally{
        graphBusy=false;
    }
}

document.querySelectorAll('[data-minutes]').forEach(button=>{
    button.addEventListener('click',()=>{
        graphMinutes=Number(button.dataset.minutes);
        updateGraph();
    });
});

let liveBusy=false;async function update(){if(liveBusy)return;liveBusy=true;try{let d=await(await fetch('/api/live')).json();document.getElementById('live').innerHTML=d.map(m=>'<div class="tile"><b>'+esc(m.name)+'</b> · '+(m.online?'ONLINE':'OFFLINE')+'<div class="grid">'+Object.entries(m.values).map(([k,v])=>'<div><small>'+esc(k)+'</small><br><strong style="color:'+esc((m.colors||{})[k]||'#ecf8f7')+'">'+esc(v)+'</strong></div>').join('')+'</div></div>').join('')||'Noch keine Miner';}catch(e){document.getElementById('live').textContent='Verbindung fehlgeschlagen'}finally{liveBusy=false}}function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}update();
updateGraph();
setInterval(update,{{ cfg.refresh_seconds|int }}*1000);
setInterval(updateGraph,10000)</script></body></html>'''
@app.get('/')
@auth
def home():
    if not config().get('setup_complete'):return redirect('/setup')
    return render_template_string(PAGE,cfg=config(),fields=FIELDS,font_options=FONT_OPTIONS,stamp=int(time.time()))
@app.post('/add')
@auth
def add():
    try: ip=miner_ip(request.form.get('ip','').strip())
    except ValueError: abort(400,'Ungültige private LAN-IP')
    name=request.form.get('name','').strip()
    if not name or len(name)>50: abort(400)
    d=config()
    if len(d['miners'])>=20 or any(m['ip']==ip for m in d['miners']): abort(400,'Doppelte IP oder Limit erreicht')
    d['miners'].append({'name':name,'ip':ip}); save(d); return redirect('/')
@app.post('/remove/<int:i>')
@auth
def remove(i):
    d=config()
    if 0<=i<len(d['miners']): d['miners'].pop(i); save(d)
    return redirect('/')
@app.post('/design')
@auth
def design():
    d=config(); d['title']=request.form.get('title','BITAXE LIVE')[:70]
    for k in ('bg','panel','text','accent'):
        v=request.form.get(k,'')
        if len(v)==7 and v[0]=='#' and all(c in '0123456789abcdefABCDEF' for c in v[1:]): d['theme'][k]=v
    d['fields']=[x for x in request.form.getlist('fields') if x in FIELDS]
    try:d['refresh_seconds']=max(2,min(300,int(request.form.get('refresh','10'))))
    except ValueError:pass
    d['font_family']=request.form.get('font_family','dejavu') if request.form.get('font_family') in FONT_OPTIONS else 'dejavu'
    for key,low,high,default in [('font_title_size',24,66,43),('font_value_size',20,58,38),('font_label_size',14,32,22)]:
        try: d[key]=max(low,min(high,int(request.form.get(key,default))))
        except (ValueError,TypeError): d[key]=default
    for key, low, high, default in [
        ('panel_opacity', 0, 100, 100),
        ('background_dim', 0, 90, 0),
    ]:
        try:
            d[key] = max(low, min(high, int(request.form.get(key, d.get(key, default)))))
        except (ValueError, TypeError):
            d[key] = default
    grid_color = request.form.get(
        'graph_grid_color', d.get('graph_grid_color', '#34444b')
    )
    if (
        len(grid_color) == 7
        and grid_color.startswith('#')
        and all(c in '0123456789abcdefABCDEF' for c in grid_color[1:])
    ):
        d['graph_grid_color'] = grid_color

    graph_color = request.form.get('graph_bg', d.get('graph_bg', '#17262b'))
    if (
        len(graph_color) == 7
        and graph_color.startswith('#')
        and all(c in '0123456789abcdefABCDEF' for c in graph_color[1:])
    ):
        d['graph_bg'] = graph_color

    try:
        d['graph_opacity'] = max(
            0, min(100, int(request.form.get(
                'graph_opacity', d.get('graph_opacity', 100)
            )))
        )
    except (ValueError, TypeError):
        d['graph_opacity'] = 100

    fit = request.form.get('background_fit', d.get('background_fit', 'cover'))
    d['background_fit'] = fit if fit in ('cover', 'contain') else 'cover'
    limits = dict(DEFAULT['temp_limits'])
    limits.update(d.get('temp_limits') or {})
    try:
        values = {
            key: int(request.form.get(key, limits[key]))
            for key in limits
        }
        if (
            all(0 <= v <= 120 for v in values.values())
            and values['asic_yellow'] < values['asic_red']
            and values['vr_yellow'] < values['vr_red']
        ):
            d['temp_limits'] = values
    except (ValueError, TypeError):
        pass
    save(d);return redirect('/')
@app.post('/discord-bots/toggle')
@auth
def discord_bot_toggle():
    bot_id = request.form.get('bot_id', '')

    with LOCK:
        d = config()
        bot = find_discord_bot(d, bot_id)

        if bot is None:
            abort(404, 'Discord-Bot nicht gefunden')

        if not bot.get('enabled', False) and not bot.get('token'):
            abort(400, 'Bitte zuerst einen Bot-Token hinterlegen')

        bot['enabled'] = not bot.get('enabled', False)
        save(d)

    return redirect('/')


@app.post('/discord-bots/commands-toggle')
@auth
def discord_bot_commands_toggle():
    bot_id = request.form.get('bot_id', '')

    with LOCK:
        d = config()
        bot = find_discord_bot(d, bot_id)

        if bot is None:
            abort(404, 'Discord-Bot nicht gefunden')

        bot['commands_enabled'] = not bot.get(
            'commands_enabled', True
        )

        save(d)

    return redirect('/')


@app.post('/discord-bots/edit')
@auth
def discord_bot_edit():
    bot_id = request.form.get('bot_id', '')
    name = request.form.get('name', '').strip()[:50]

    if not name:
        abort(400, 'Bot-Name erforderlich')

    token = request.form.get('token', '').strip()
    channel = request.form.get('channel', '').strip()
    guild = request.form.get('guild', '').strip()
    roles = request.form.get('roles', '').strip()[:300]

    with LOCK:
        d = config()
        bot = find_discord_bot(d, bot_id)

        if bot is None:
            abort(404, 'Discord-Bot nicht gefunden')

        bot['name'] = name

        if token and token != bot.get('token', ''):
            bot['token'] = token
            bot['dashboard_message_id'] = ''
            bot['synced_guild_id'] = ''
            bot.pop('synced_commands_enabled', None)

        if channel != bot.get('channel_id', ''):
            bot['channel_id'] = channel
            bot['dashboard_message_id'] = ''

        bot['guild_id'] = guild
        bot['admin_role_ids'] = roles

        save(d)

    return redirect('/')

@app.post('/discord-bots/add')
@auth
def discord_bot_add():
    name = request.form.get('name', '').strip()[:50]

    if not name:
        abort(400, 'Bot-Name erforderlich')

    with LOCK:
        d = config()

        if len(d.get('discord_bots', [])) >= 10:
            abort(400, 'Maximal 10 Discord-Bots erlaubt')

        create_discord_bot(d, name)
        save(d)

    return redirect('/')

@app.post('/discord')
@auth
def discord_settings():
    token = request.form.get('token', '').strip()
    channel = request.form.get('channel', '').strip()[:300]
    guild = request.form.get('guild', '').strip()[:300]
    roles = request.form.get('roles', '').strip()[:300]

    with LOCK:
        d = config()
        bot = find_discord_bot(d, 'bot1')

        if bot is None:
            abort(404, 'Hauptbot nicht gefunden')

        if token and token != bot.get('token', ''):
            bot['token'] = token
            bot['dashboard_message_id'] = ''
            bot['synced_guild_id'] = ''
            bot.pop('synced_commands_enabled', None)

        if channel != bot.get('channel_id', ''):
            bot['channel_id'] = channel
            bot['dashboard_message_id'] = ''

        bot['guild_id'] = guild
        bot['admin_role_ids'] = roles

        save(d)

    return redirect('/')
@app.post('/logo')
@auth
def logo():
    f=request.files.get('logo')
    if not f: abort(400)
    raw=f.stream.read(2*1024*1024+1)
    if len(raw)>2*1024*1024: abort(413)
    try:
        im=Image.open(io.BytesIO(raw)); im.verify(); im=Image.open(io.BytesIO(raw)).convert('RGBA'); im.thumbnail((128,128)); im.save(ROOT/'logo.png')
    except Exception: abort(400,'Ungültiges Bild')
    d=config();d['logo']=True;save(d);return redirect('/')
@app.post('/background-upload')
@auth
def background_upload():
    f = request.files.get('background')
    if not f or not f.filename:
        abort(400, 'Kein Hintergrundbild ausgewählt')

    raw = f.stream.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        abort(413, 'Bild ist größer als 5 MB')

    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in ('PNG', 'JPEG'):
                abort(400, 'Nur PNG und JPG erlaubt')
            source.verify()

        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > 20000000:
                abort(400, 'Bildauflösung zu groß')
            picture = source.convert('RGB')
            picture.thumbnail((1920, 1080))
            picture.save(ROOT / 'background.png', format='PNG')

    except (OSError, ValueError, TypeError):
        abort(400, 'Ungültiges Hintergrundbild')

    d = config()
    d['background_enabled'] = True
    save(d)
    return redirect('/')


@app.post('/background-remove')
@auth
def background_remove():
    (ROOT / 'background.png').unlink(missing_ok=True)
    d = config()
    d['background_enabled'] = False
    save(d)
    return redirect('/')


@app.post('/reset-block')
@auth
def reset_block():
    d=config();d['block_alert']=None;save(d);return redirect('/')
@app.get('/api/live')
@auth
def live():
    miners = scan()
    record_history(miners)
    return jsonify([{'name':m['name'],'ip':m['ip'],'online':m['online'],'values':{FIELDS.get(k,k):v for k,v in m['values'].items()},'colors':{FIELDS.get(k,k):v for k,v in m['colors'].items()}} for m in miners])


@app.get('/api/history')
@auth
def history_api():
    try:
        minutes = int(request.args.get('minutes', 5))
    except (TypeError, ValueError):
        minutes = 5
    if minutes not in (5, 15, 60, 1440):
        minutes = 5

    cutoff = int(time.time()) - minutes * 60

    with HISTORY_LOCK:
        try:
            history = json.loads(HISTORY_FILE.read_text())
            if not isinstance(history, dict):
                history = {}
        except (OSError, ValueError):
            history = {}

    result = {}
    for miner in config()['miners']:
        ip = miner['ip']
        points = history.get(ip, [])
        if not isinstance(points, list):
            points = []
        result[ip] = [
            point for point in points
            if isinstance(point, dict)
            and isinstance(point.get('time'), (int, float))
            and point['time'] >= cutoff
        ]

    return jsonify(result)

FONT_OPTIONS={
    'dejavu':'DejaVu Sans (Modern)',
    'dejavu-bold':'DejaVu Sans Bold (Kräftig)',
    'dejavu-mono':'DejaVu Sans Mono (Technisch)',
    'liberation-sans':'Liberation Sans (Elegant)',
    'liberation-serif':'Liberation Serif (Klassisch)',
}
FONT_PATHS={
    'dejavu':'/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    'dejavu-bold':'/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    'dejavu-mono':'/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
    'liberation-sans':'/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf',
    'liberation-serif':'/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf',
}
def font(sz,family='dejavu'):
    for p in (FONT_PATHS.get(family,FONT_PATHS['dejavu']),FONT_PATHS['dejavu']):
        try:return ImageFont.truetype(p,int(sz))
        except OSError:pass
    return ImageFont.load_default()
def fit_text(draw,text,max_width,size,family,min_size=12):
    text=str(text)
    while size>min_size and draw.textbbox((0,0),text,font=font(size,family))[2]>max_width:
        size-=1
    if draw.textbbox((0,0),text,font=font(size,family))[2]>max_width:
        while text and draw.textbbox((0,0),text+'…',font=font(size,family))[2]>max_width:
            text=text[:-1]
        text+='…'
    return text,font(size,family)
def draw_discord_graph(image, draw, miner_ip, x, y, width, height, theme, family, cfg=None):
    """Zeichnet echte Messwerte der letzten 15 Minuten in ein Discord-PNG."""
    cfg = cfg or {}
    graph_color = cfg.get('graph_bg', '#17262b')
    graph_opacity = cfg.get('graph_opacity', 100)
    graph_grid_color = cfg.get('graph_grid_color', '#34444b')

    draw_transparent_card(
        image,
        (x, y, x + width, y + height),
        16,
        graph_color,
        graph_opacity
    )
    draw.text(
        (x + 20, y + 14),
        'LIVE GRAPH · 15 MIN',
        fill=theme['text'],
        font=font(23, family)
    )

    cutoff = time.time() - 900
    with HISTORY_LOCK:
        try:
            history = json.loads(HISTORY_FILE.read_text())
            points = history.get(miner_ip, []) if isinstance(history, dict) else []
        except (OSError, ValueError):
            points = []

    points = [
        point for point in points
        if isinstance(point, dict)
        and isinstance(point.get('time'), (int, float))
        and cutoff <= point['time'] <= time.time() + 30
        and isinstance(point.get('values'), dict)
    ]

    left, right = x + 54, x + width - 22
    top, bottom = y + 65, y + height - 47

    for fraction in (0, 0.25, 0.5, 0.75, 1):
        yy = int(top + (bottom - top) * fraction)
        draw.line((left, yy, right, yy), fill=graph_grid_color, width=1)

    if len(points) < 2:
        draw.text(
            (left + 20, top + 35),
            'Noch nicht genug Messwerte',
            fill=theme['text'],
            font=font(22, family)
        )
        return

    series = (
        ('hashrate_ghs', '#86efac', 'Hashrate'),
        ('device_power_w', '#e7b66e', 'Watt'),
        ('asic_temp_c', '#facc15', 'ASIC'),
        ('vr_temp_c', '#fb7185', 'VR'),
    )

    for index, (key, color, label) in enumerate(series):
        values = [
            graph_number(point['values'].get(key))
            for point in points
        ]
        valid = [value for value in values if value is not None]
        if len(valid) < 2:
            continue

        low, high = min(valid), max(valid)
        padding = max((high - low) * 0.15, abs(high) * 0.02, 0.1)
        low -= padding
        high += padding

        previous = None
        for point, value in zip(points, values):
            if value is None:
                previous = None
                continue
            px = int(left + (right - left) * (point['time'] - cutoff) / 900)
            px = max(left, min(right, px))
            py = int(bottom - (value - low) / (high - low) * (bottom - top))
            if previous is not None:
                draw.line((previous[0], previous[1], px, py), fill=color, width=3)
            previous = (px, py)

        legend_x = left + index * 205
        draw.line(
            (legend_x, y + height - 23, legend_x + 24, y + height - 23),
            fill=color, width=4
        )
        draw.text(
            (legend_x + 32, y + height - 36),
            label,
            fill=theme['text'],
            font=font(17, family)
        )

def dashboard_background(width, height, cfg):
    theme = cfg['theme']
    background = Image.new('RGB', (width, height), theme['bg'])

    if not cfg.get('background_enabled'):
        return background

    path = ROOT / 'background.png'
    if not path.is_file():
        return background

    try:
        with Image.open(path) as source:
            fit = cfg.get('background_fit', 'cover')
            if fit == 'contain':
                picture = ImageOps.contain(
                    source.convert('RGB'), (width, height)
                )
                x = (width - picture.width) // 2
                y = (height - picture.height) // 2
                background.paste(picture, (x, y))
            else:
                background = ImageOps.fit(
                    source.convert('RGB'), (width, height)
                )

        dim = max(0, min(90, int(cfg.get('background_dim', 0))))
        if dim:
            overlay = Image.new('RGB', (width, height), '#000000')
            background = Image.blend(background, overlay, dim / 100)

    except (OSError, ValueError, TypeError) as error:
        logging.warning("Hintergrundbild konnte nicht geladen werden: %s", error)

    return background


def draw_transparent_card(image, box, radius, color, opacity):
    opacity = max(0, min(100, int(opacity)))

    if opacity == 100:
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(box, radius=radius, fill=color)
        return

    from PIL import ImageColor

    x1, y1, x2, y2 = map(int, box)
    width = x2 - x1 + 1
    height = y2 - y1 + 1

    overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    rgb = ImageColor.getrgb(color)

    overlay_draw.rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=radius,
        fill=(*rgb, round(opacity * 255 / 100))
    )

    region = image.crop((x1, y1, x2 + 1, y2 + 1)).convert('RGBA')
    result = Image.alpha_composite(region, overlay).convert('RGB')
    image.paste(result, (x1, y1))


def image_dashboard(index=None):
    d=config(); miners=scan(); alert=d.get('block_alert')
    if alert: index=next((i for i,m in enumerate(miners) if m['ip']==alert['ip']),0)
    elif index is None: index=d.get('selected',0)
    m=miners[index] if miners and 0<=index<len(miners) else None
    theme=d['theme']; family=d.get('font_family','dejavu')
    title_size=max(24,min(66,int(d.get('font_title_size',43))))
    value_size=max(20,min(58,int(d.get('font_value_size',38))))
    label_size=max(14,min(32,int(d.get('font_label_size',22))))
    fixed_keys={'hashrate_ghs','best_share_all_time','accepted_shares','rejected_shares'}
    raw=m.get('raw') if m else None
    raw=raw if isinstance(raw,dict) else {}
    hero_vals=[
        ('hashrate_ghs',formatted('hashrate_ghs',raw)),
        ('best_share_all_time',formatted('best_share_all_time',raw)),
        ('shares',{
            'accepted_shares':formatted('accepted_shares',raw),
            'rejected_shares':formatted('rejected_shares',raw)
        })
    ]
    detail_vals=[
        (k,v) for k,v in (m['values'] if m else {}).items()
        if k not in fixed_keys and k != 'firmware_version'
    ]
    cols=5
    rows=(len(detail_vals)+cols-1)//cols
    card_h=104
    hero_h=140 if hero_vals else 0
    hero_space=hero_h+20 if hero_vals else 0
    top=184+(108 if alert else 0)
    graph_h=280
    graph_gap=20
    graph_y=top+hero_space+rows*(card_h+12)+graph_gap
    height=max(690,graph_y+graph_h+30)
    im=dashboard_background(1000,height,d); draw=ImageDraw.Draw(im)
    heading,heading_font=fit_text(draw,d['title'],750,title_size,family,20)
    draw.text((36,27),heading,fill=theme['accent'],font=heading_font)
    logo_path=ROOT/'logo.png'
    if logo_path.exists():
        try:
            with Image.open(logo_path) as original:
                logo=original.convert('RGBA');logo.thumbnail((124,124))
                im.paste(logo,(1000-30-logo.width,20),logo)
        except Exception:pass
    firmware = str(raw.get('firmware_version') or '').strip()
    if firmware:
        fw_text, fw_font = fit_text(
            draw, 'Firmware ' + firmware, 190, 16, family, 12
        )
        fw_width = draw.textlength(fw_text, font=fw_font)
        draw.text(
            (970 - fw_width, 151),
            fw_text,
            fill='#a1b5b7',
            font=fw_font
        )
    y=100
    if alert:
        draw.rounded_rectangle((28,y,972,y+88),radius=13,fill='#45351b',outline='#ffd166',width=3)
        alert_text,alert_font=fit_text(draw,'BLOCK FOUND!  '+alert['name'],885,35,family,19)
        draw.text((48,y+21),alert_text,fill='#ffd166',font=alert_font)
        y+=108
    status=(m['name']+' · '+('ONLINE' if m['online'] else 'OFFLINE')) if m else 'NO MINERS'
    status_text,status_font=fit_text(draw,status,910,29,family,17)
    draw.text((36,y),status_text,fill=theme['text'],font=status_font)
    y+=78
    margin=30;gap=14
    if hero_vals:
        hero_gap=14
        hero_w=(1000-2*margin-(len(hero_vals)-1)*hero_gap)//len(hero_vals)
        for n,(k,v) in enumerate(hero_vals):
            x=margin+n*(hero_w+hero_gap)
            draw_transparent_card(im,(x,y,x+hero_w,y+hero_h),18,theme['panel'],d.get('panel_opacity',100))
            if k=='shares':
                label,lf=fit_text(draw,'SHARES',hero_w-36,24,family,15)
                draw.text((x+20,y+20),label,fill=theme['text'],font=lf)
                accepted=v.get('accepted_shares','–')
                rejected=v.get('rejected_shares','–')
                combined=f'{accepted} / {rejected}'
                value,vf=fit_text(draw,combined,hero_w-36,48,family,18)
                ax=x+20
                sy=y+65
                draw.text((ax,sy),str(accepted),fill='#86efac',font=vf)
                ax+=draw.textlength(str(accepted),font=vf)
                draw.text((ax,sy),' / ',fill=theme['text'],font=vf)
                ax+=draw.textlength(' / ',font=vf)
                draw.text((ax,sy),str(rejected),fill='#fb7185',font=vf)
            else:
                label,lf=fit_text(draw,FIELDS.get(k,k).upper(),hero_w-36,24,family,15)
                value,vf=fit_text(draw,v,hero_w-36,58,family,22)
                draw.text((x+20,y+20),label,fill=theme['text'],font=lf)
                draw.text((x+20,y+65),value,
                          fill=(m.get('colors') or {}).get(k,theme['accent']),font=vf)
        y+=hero_space
    card_w=(1000-2*margin-(cols-1)*gap)//cols
    for n,(k,v) in enumerate(detail_vals):
        col=n%cols;row=n//cols;x=margin+col*(card_w+gap);yy=y+row*(card_h+12)
        draw_transparent_card(im,(x,yy,x+card_w,yy+card_h),14,theme['panel'],d.get('panel_opacity',100))
        label,lf=fit_text(draw,FIELDS.get(k,k),card_w-30,label_size,family,13)
        value,vf=fit_text(draw,v,card_w-30,value_size,family,17)
        draw.text((x+16,yy+13),label,fill=theme['text'],font=lf)
        draw.text((x+16,yy+card_h-15-vf.getbbox(value)[3]),value,fill=(m.get('colors') or {}).get(k,theme['accent']),font=vf)
    if m:
        draw_discord_graph(im,draw,m['ip'],margin,graph_y,1000-2*margin,graph_h,theme,family,d)
    out=io.BytesIO();im.save(out,format='PNG');out.seek(0);return out
@app.get('/preview.png')
@auth
def preview():return send_file(image_dashboard(),mimetype='image/png',max_age=0)

# Setup wizard and settings (dashboard authentication applies to all routes).
SETUP_PAGE="""<!doctype html><html lang="{{lang}}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Bitaxe Live Setup</title><style>body{margin:0;background:radial-gradient(circle at top,#26305a,#0b111b 65%);color:#f0f8ff;font:16px system-ui}main{max-width:740px;margin:45px auto;padding:24px}section{background:#17242adf;border:1px solid #3b5260;border-radius:18px;padding:24px;margin-bottom:20px}h1{color:#49e5c4}label{display:block;margin:15px 0}input,select{display:block;width:100%;box-sizing:border-box;background:#0b1116;color:white;border:1px solid #637780;border-radius:9px;padding:12px;margin-top:6px}button,.btn{display:inline-block;background:#46e0c2;color:#09141a;border:0;border-radius:9px;padding:12px 20px;font-weight:bold;cursor:pointer;text-decoration:none}a{color:#5aeacb}small,p,li{line-height:1.65}.hint{color:#bacbd2}code{color:#f6c979}nav{display:flex;gap:12px;flex-wrap:wrap}</style></head><body><main><h1>⛏️ BITAXE LIVE</h1><p class="hint">{{t.welcome}}</p><nav><a href="/language?lang=de">🇩🇪 Deutsch</a><a href="/language?lang=en">🇬🇧 English</a></nav><section><h2>1 · {{t.language}}</h2><p>{{t.language_help}}</p><form method="post" action="/language"><select name="lang"><option value="de" {% if lang=='de' %}selected{% endif %}>Deutsch</option><option value="en" {% if lang=='en' %}selected{% endif %}>English</option></select><button>{{t.save_language}}</button></form></section><section><h2>2 · {{t.miner}}</h2><form method="post" action="/setup-miner"><label>{{t.miner_name}}<input name="name" value="{{miner.name if miner else 'TERROR'}}" maxlength="50" required></label><label>{{t.miner_ip}}<input name="ip" value="{{miner.ip if miner else ''}}" placeholder="192.168.178.24" required></label><button>{{t.save_miner}}</button></form></section><section><h2>3 · Discord</h2><p><a href="https://discord.com/developers/applications" target="_blank" rel="noopener noreferrer">Discord Developer Portal ↗</a></p><ol>{% for instruction in t.instructions %}<li>{{instruction}}</li>{% endfor %}</ol><p class="hint">{{t.permissions}}</p><p class="hint">{{t.invite}}</p><form method="post" action="/setup-discord"><label>{{t.token}}<input name="token" type="password" autocomplete="new-password" placeholder="{{t.token_hint}}"></label><label>{{t.guild}}<input name="guild" value="{{main_bot.get('guild_id', '')}}" inputmode="numeric"></label><label>{{t.channel}}<input name="channel" value="{{main_bot.get('channel_id', '')}}" inputmode="numeric"></label><label>{{t.roles}}<input name="roles" value="{{main_bot.get('admin_role_ids', '')}}"></label><button>{{t.save_bot}}</button></form></section><section><h2>4 · {{t.finish}}</h2><p>{{t.ready}}</p><p>Miner: <b>{{cfg.miners|length}}</b> · Discord: <b>{{'✓' if main_bot.get('token') else '✗'}}</b></p><form method="post" action="/finish-setup"><button>{{t.start_bot}}</button></form><p><a href="/">{{t.home}}</a></p></section></main></body></html>"""
TRANSLATIONS={
 'de':dict(welcome='Einrichtung in vier Schritten. Einstellungen können später jederzeit geändert werden.',language='Sprache auswählen',language_help='Diese Sprache wird für den Einrichtungsassistenten verwendet.',save_language='Sprache speichern',miner='Miner einrichten',miner_name='Miner-Name',miner_ip='Private LAN-IP des Miners',save_miner='Miner speichern',instructions=['Neue Anwendung anlegen und unter Bot einen Bot erstellen.','Im Bot-Bereich den Token erzeugen und sicher kopieren. Token niemals im Chat veröffentlichen.','Unter OAuth2 → URL Generator die Scopes bot und applications.commands wählen.','Bot-Berechtigungen: View Channels, Send Messages, Embed Links, Attach Files und Read Message History. Administrator-Berechtigung ist für den Bot nicht nötig.','Mit dem erzeugten OAuth2-Link den Bot auf deinen Discord-Server einladen.','Entwicklermodus in Discord einschalten und Server-ID, Kanal-ID sowie optional Admin-Rollen-IDs kopieren.','Die Slash-Commands sind nur für Discord-Administratoren oder eingetragene Admin-Rollen ausführbar. Unter Integrationen können sie für @everyone zusätzlich deaktiviert werden.'],permissions='Der Bot braucht keine Administrator-Rechte. Für die Miner-Steuerung gelten eigene Admin-Prüfungen und private Bestätigungsbuttons.',invite='Der OAuth2-Einladungslink wird im Developer Portal erzeugt. Bitte nur den eigenen Bot einladen.',token='Bot-Token',token_hint='Leer lassen = gespeicherten Token behalten',guild='Server-ID (Guild ID)',channel='Dashboard-Kanal-ID',roles='Admin-Rollen-IDs (kommagetrennt, optional)',save_bot='Bot-Einstellungen speichern',finish='Abschluss',ready='Speichere zuerst Miner und Bot. Danach kann der Bot gestartet werden.',start_bot='Einrichtung abschließen und Bot starten',home='Zum Dashboard'),
 'en':dict(welcome='Set up your dashboard in four steps. You can change these settings later.',language='Choose language',language_help='This language is used for the setup wizard.',save_language='Save language',miner='Configure miner',miner_name='Miner name',miner_ip='Miner private LAN IP',save_miner='Save miner',instructions=['Create a new application, then create a bot in the Bot section.','Generate a token in the Bot section and keep it secret. Never share your token in chat.','Under OAuth2 → URL Generator, select the bot and applications.commands scopes.','Bot permissions: View Channels, Send Messages, Embed Links, Attach Files and Read Message History. The bot does not need Administrator permission.','Invite your bot to your Discord server using the generated OAuth2 URL.','Enable Discord Developer Mode and copy your server ID, channel ID and optional admin role IDs.','Slash commands are limited to Discord administrators or configured admin roles. You can also disable commands for @everyone in Integrations.'],permissions='The bot does not need Administrator permission. Miner control uses separate admin checks and private confirmation buttons.',invite='Generate the OAuth2 invitation URL in the Developer Portal. Only invite your own bot.',token='Bot token',token_hint='Leave blank to keep saved token',guild='Server ID (Guild ID)',channel='Dashboard channel ID',roles='Admin role IDs (comma-separated, optional)',save_bot='Save bot settings',finish='Finish',ready='Save a miner and bot settings first, then start the bot.',start_bot='Finish setup and start bot',home='Go to dashboard')}
@app.get('/setup')
@auth
def setup():
    d = config()
    lang = d.get('language', 'de')
    main_bot = find_discord_bot(d, 'bot1') or {}

    return render_template_string(
        SETUP_PAGE,
        lang=lang,
        t=TRANSLATIONS.get(lang, TRANSLATIONS['de']),
        cfg=d,
        miner=d['miners'][0] if d['miners'] else None,
        main_bot=main_bot
    )
@app.route('/language',methods=['GET','POST'])
@auth
def language():
    d=config();v=(request.form if request.method=='POST' else request.args).get('lang','de');d['language']=v if v in TRANSLATIONS else 'de';save(d);return redirect('/setup')
@app.post('/setup-miner')
@auth
def setup_miner():
    try:ip=miner_ip(request.form.get('ip','').strip())
    except ValueError:abort(400,'Invalid private LAN IP')
    name=request.form.get('name','').strip()[:50]
    if not name:abort(400,'Miner name required')
    d=config()
    if d['miners']:d['miners'][0]={'name':name,'ip':ip}
    else:d['miners'].append({'name':name,'ip':ip})
    save(d);return redirect('/setup')
@app.post('/setup-discord')
@auth
def setup_discord():
    token = request.form.get('token', '').strip()
    guild = request.form.get('guild', '').strip()[:300]
    channel = request.form.get('channel', '').strip()[:300]
    roles = request.form.get('roles', '').strip()[:300]

    with LOCK:
        d = config()
        bot = find_discord_bot(d, 'bot1')

        if bot is None:
            bot = create_discord_bot(d, 'Hauptbot')
            bot['id'] = 'bot1'

        if token and token != bot.get('token', ''):
            bot['token'] = token
            bot['dashboard_message_id'] = ''
            bot['synced_guild_id'] = ''
            bot.pop('synced_commands_enabled', None)

        if channel != bot.get('channel_id', ''):
            bot['channel_id'] = channel
            bot['dashboard_message_id'] = ''

        bot['guild_id'] = guild
        bot['admin_role_ids'] = roles

        d['discord_bots_migrated'] = True
        save(d)

    return redirect('/setup')

@app.post('/finish-setup')
@auth
def finish_setup():
    with LOCK:
        d = config()

        if not d['miners']:
            abort(400, 'At least one miner required')

        d['setup_complete'] = True

        bot = find_discord_bot(d, 'bot1')

        if bot is not None and bot.get('token'):
            bot['enabled'] = True

        save(d)

    return redirect('/')
@app.post('/bot-toggle')
@auth
def bot_toggle():
    with LOCK:
        d = config()
        bot = find_discord_bot(d, 'bot1')

        if bot is None:
            abort(404, 'Hauptbot nicht gefunden')

        if not bot.get('enabled', False) and not bot.get('token'):
            abort(400, 'Bitte zuerst einen Bot-Token hinterlegen')

        bot['enabled'] = not bot.get('enabled', False)
        save(d)

    return redirect('/')
@app.get('/settings')
@auth
def settings():return redirect('/setup')

# Discord bot is optional and runs in its own background thread.
def discord_bot_worker(bot_id):
    import asyncio, discord
    from discord import app_commands

    while True:
        bot = current_discord_bot(bot_id)

        if bot is None:
            return

        token = bot.get('token', '')

        if not token or not bot.get('enabled', False):
            return
        intents=discord.Intents.default(); client=discord.Client(intents=intents); tree=app_commands.CommandTree(client)
        def allowed(inter):
            current = current_discord_bot(bot_id)

            if current is None or not current.get('enabled', False):
                return False

            if not current.get('commands_enabled', True):
                return False

            permissions = getattr(inter.user, 'guild_permissions', None)
            if permissions and permissions.administrator:
                return True

            ids = {
                x.strip()
                for x in current.get('admin_role_ids', '').split(',')
                if x.strip()
            }

            return any(
                str(role.id) in ids
                for role in getattr(inter.user, 'roles', [])
            )
        async def deny(inter):await inter.response.send_message('Nur für autorisierte Admins.',ephemeral=True)
        async def show(inter,idx=None):
            if not allowed(inter):return await deny(inter)
            await inter.response.defer(ephemeral=True)
            ms=config()['miners']
            if idx is not None and idx>=len(ms):return await inter.followup.send('Miner nicht vorhanden.',ephemeral=True)
            await inter.followup.send(file=discord.File(image_dashboard(idx),'bitaxe-live.png'),ephemeral=True)
        @tree.command(name='dashboard',description='Haupt-Dashboard anzeigen')
        async def dashboard(inter:discord.Interaction):await show(inter)
        @tree.command(name='miners',description='Alle Miner auflisten')
        async def miners(inter:discord.Interaction):
            if not allowed(inter):return await deny(inter)
            ms=scan(); await inter.response.send_message('\n'.join(f"{i+1}. {m['name']} {'ONLINE' if m['online'] else 'OFFLINE'}" for i,m in enumerate(ms)) or 'Keine Miner',ephemeral=True)
        for n in range(1,21):
            def make_specific(number):
                async def specific(inter:discord.Interaction): await show(inter,number-1)
                return specific
            tree.command(name=f'miner{n}',description=f'Miner {n} anzeigen')(make_specific(n))
        # Device controls: administrator-only, private confirmation, one action per miner.
        action_locks={}
        action_paths={'start':'/api/asic/start','pause':'/api/asic/stop','restart':'/api/reboot'}
        class ConfirmControl(discord.ui.View):
            def __init__(self, owner_id, index, name, ip, action):
                super().__init__(timeout=60)
                self.owner_id=owner_id;self.index=index;self.name=name;self.ip=ip;self.action=action
            async def interaction_check(self, interaction):
                if interaction.user.id!=self.owner_id or not allowed(interaction):
                    await interaction.response.send_message('Not authorized / Nicht berechtigt.',ephemeral=True)
                    return False
                return True
            @discord.ui.button(label='Bestätigen / Confirm',style=discord.ButtonStyle.danger)
            async def confirm(self,interaction:discord.Interaction,button:discord.ui.Button):
                await interaction.response.defer(ephemeral=True)
                lock=action_locks.setdefault(self.ip,asyncio.Lock())
                if lock.locked():
                    await interaction.followup.send('Ein Befehl läuft bereits / Command already running.',ephemeral=True);return
                async with lock:
                    # Re-check selected device has not changed between command and confirmation.
                    ms=config()['miners']
                    if self.index>=len(ms) or ms[self.index]['ip']!=self.ip:
                        await interaction.followup.send('Miner-Konfiguration geändert / Miner changed.',ephemeral=True);return
                    try:
                        def send_command():
                            response=requests.post('http://'+miner_ip(self.ip)+action_paths[self.action],json={},timeout=12)
                            response.raise_for_status()
                            result=response.json()
                            if not isinstance(result,dict) or result.get('ok') is False:
                                raise ValueError(result.get('output',result.get('error','Miner rejected command')) if isinstance(result,dict) else 'Invalid response')
                            return str(result.get('output','Command accepted'))[:500]
                        result=await asyncio.to_thread(send_command)
                        await interaction.followup.send('✅ '+self.name+': '+result,ephemeral=True)
                    except Exception as exc:
                        logging.exception('HarloOS control failed')
                        await interaction.followup.send('❌ '+self.name+': '+str(exc)[:300],ephemeral=True)
                for child in self.children:child.disabled=True
                try:await interaction.message.edit(view=self)
                except Exception:pass
            @discord.ui.button(label='Abbrechen / Cancel',style=discord.ButtonStyle.secondary)
            async def cancel(self,interaction:discord.Interaction,button:discord.ui.Button):
                for child in self.children:child.disabled=True
                await interaction.response.edit_message(content='Abgebrochen / Cancelled.',view=self)
            async def on_timeout(self):
                for child in self.children:child.disabled=True
        for cmd in ('start','pause','restart'):
            def make_control(operation):
                async def control(inter:discord.Interaction,miner:int):
                    if not allowed(inter):return await deny(inter)
                    ms=config()['miners']
                    if miner<1 or miner>len(ms):
                        return await inter.response.send_message('Miner nicht vorhanden / Miner not found.',ephemeral=True)
                    target=ms[miner-1]
                    if not telemetry(target['ip']):
                        return await inter.response.send_message('Miner offline. Steuerung nicht gesendet / Miner offline.',ephemeral=True)
                    v=ConfirmControl(inter.user.id,miner-1,target['name'],target['ip'],operation)
                    await inter.response.send_message(f"⚠️ {operation.upper()} · {target['name']} ({target['ip']})?",view=v,ephemeral=True)
                return control
            tree.command(name=cmd,description='Miner control / Miner steuern (confirmation required)')(make_control(cmd))
        command_sync_lock = asyncio.Lock()

        async def sync_bot_commands():
            async with command_sync_lock:
                return await sync_bot_commands_locked()

        async def sync_bot_commands_locked():
            current = current_discord_bot(bot_id)

            if current is None:
                return False

            enabled = bool(current.get('commands_enabled', True))
            guild_id = str(current.get('guild_id', '')).strip()
            last_synced_guild = str(
                current.get('synced_guild_id', '')
            ).strip()

            try:
                guild = (
                    discord.Object(id=int(guild_id))
                    if guild_id else None
                )

                if last_synced_guild and last_synced_guild != guild_id:
                    old_guild = discord.Object(
                        id=int(last_synced_guild)
                    )
                    tree.clear_commands(guild=old_guild)
                    await tree.sync(guild=old_guild)

                if guild is not None:
                    if enabled:
                        tree.copy_global_to(guild=guild)
                    else:
                        tree.clear_commands(guild=guild)

                    await tree.sync(guild=guild)

                    # Globale Befehle entfernen, damit sie nicht
                    # zusätzlich auf anderen Servern erscheinen.
                    saved_commands = tree.get_commands()
                    tree.clear_commands(guild=None)

                    try:
                        await tree.sync()
                    finally:
                        for command in saved_commands:
                            tree.add_command(command)

                else:
                    saved_commands = tree.get_commands()

                    if not enabled:
                        tree.clear_commands(guild=None)

                    try:
                        await tree.sync()
                    finally:
                        if not enabled:
                            for command in saved_commands:
                                tree.add_command(command)

                if not save_discord_sync_state(bot_id, token, guild_id, enabled):
                    logging.warning(
                        'Discord sync state could not be saved for %s',
                        bot_id
                    )
                    return False

                logging.info(
                    'Discord commands synced: bot=%s enabled=%s guild=%s',
                    bot_id,
                    enabled,
                    guild_id or 'global'
                )

                return True

            except Exception:
                logging.exception(
                    'Discord command sync failed for %s',
                    bot_id
                )
                return False

        async def periodic():
            await client.wait_until_ready()

            cached_message = None
            cached_channel_id = None

            while not client.is_closed():
                try:
                    cfg = config()
                    bot = current_discord_bot(bot_id)

                    if (
                        bot is None
                        or not bot.get('enabled', False)
                        or bot.get('token', '') != token
                    ):
                        await client.close()
                        break

                    cid = str(bot.get('channel_id', '')).strip()
                    mid = str(bot.get('dashboard_message_id', '')).strip()

                    if cid:
                        channel = (
                            client.get_channel(int(cid))
                            or await client.fetch_channel(int(cid))
                        )

                        if (
                            cached_channel_id != cid
                            or (
                                cached_message is not None
                                and str(cached_message.id) != mid
                            )
                        ):
                            cached_message = None
                            cached_channel_id = cid

                        if mid:
                            try:
                                if cached_message is None:
                                    cached_message = await channel.fetch_message(
                                        int(mid)
                                    )

                                upload_start = time.monotonic()

                                await cached_message.edit(
                                    content=cfg['title'],
                                    attachments=[
                                        discord.File(
                                            image_dashboard(),
                                            'dashboard.png'
                                        )
                                    ]
                                )

                                logging.info(
                                    'Discord dashboard %s updated in %.2fs',
                                    bot_id,
                                    time.monotonic() - upload_start
                                )

                            except discord.NotFound:
                                mid = ''
                                cached_message = None

                        if not mid:
                            cached_message = await channel.send(
                                content=cfg['title'],
                                file=discord.File(
                                    image_dashboard(),
                                    'dashboard.png'
                                )
                            )

                            save_discord_message_id(
                                bot_id,
                                token,
                                cid,
                                str(cached_message.id)
                            )

                except Exception:
                    logging.exception(
                        'Discord dashboard update failed for %s',
                        bot_id
                    )

                await asyncio.sleep(
                    max(5, config().get('refresh_seconds', 10))
                )

        async def command_watch():
            await client.wait_until_ready()

            previous = None

            while not client.is_closed():
                current = current_discord_bot(bot_id)

                if (
                    current is None
                    or not current.get('enabled', False)
                    or current.get('token', '') != token
                ):
                    logging.info(
                        'Discord bot %s stopping after configuration change',
                        bot_id
                    )
                    await client.close()
                    return

                enabled = bool(
                    current.get('commands_enabled', True)
                )
                guild_id = str(
                    current.get('guild_id', '')
                ).strip()

                state = (enabled, guild_id)

                if state != previous:
                    success = await sync_bot_commands()

                    if success:
                        previous = state
                    else:
                        logging.warning(
                            'Discord command sync retry scheduled: %s',
                            bot_id
                        )

                await asyncio.sleep(5)

        @client.event
        async def setup_hook():
            client.loop.create_task(periodic())
            client.loop.create_task(command_watch())
        try:
            client.run(token, log_handler=None)
        except Exception:
            logging.exception(
                'Discord bot %s disconnected or failed',
                bot_id
            )

        current = current_discord_bot(bot_id)

        if current is None:
            return

        if (
            not current.get('enabled', False)
            or current.get('token', '') != token
        ):
            continue

        time.sleep(30)

def discord_manager():
    workers = {}

    while True:
        try:
            cfg = config()

            for bot in cfg.get('discord_bots', []):
                bot_id = bot.get('id')

                if not bot_id:
                    continue

                if not bot.get('enabled', False):
                    continue

                if not bot.get('token', ''):
                    continue

                thread = workers.get(bot_id)

                if thread is None or not thread.is_alive():
                    thread = threading.Thread(
                        target=discord_bot_worker,
                        args=(bot_id,),
                        daemon=True,
                        name='discord-' + str(bot_id)
                    )

                    workers[bot_id] = thread
                    thread.start()

                    logging.info(
                        'Discord worker started for bot %s',
                        bot_id
                    )

            workers = {
                bot_id: thread
                for bot_id, thread in workers.items()
                if thread.is_alive()
            }

        except Exception:
            logging.exception('Discord manager failed')

        time.sleep(5)


def history_worker():
    # Sammelt Messwerte unabhängig vom Browser alle 10 Sekunden.
    while True:
        try:
            record_history(scan())
        except Exception:
            logging.exception('Background history collection failed')
        time.sleep(2)

if __name__=='__main__':
    threading.Thread(target=history_worker,daemon=True).start()
    threading.Thread(target=discord_manager,daemon=True).start()
    app.run(host='0.0.0.0',port=8085,threaded=True)
