import csv
import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TZ = ZoneInfo(os.environ.get('FUSO', 'America/Campo_Grande'))
DATA = os.path.join(BASE, 'data')
LANCAMENTOS = os.path.join(DATA, 'lancamentos.csv')
ORCAMENTO = os.path.join(DATA, 'orcamento.json')
RECORRENTES = os.path.join(DATA, 'recorrentes.json')
SEMANAS = os.path.join(DATA, 'semanas.json')
PENDENCIAS = os.path.join(DATA, 'pendencias.json')
STATE = os.path.join(DATA, 'state.json')
REVISAR = os.path.join(DATA, 'revisar.csv')
FILA = os.path.join(DATA, 'fila.json')
INBOX = os.path.join(BASE, 'inbox')
LIMITE_AUDIO = 18 * 1024 * 1024
MAX_TENTATIVAS = 3
FECHAMENTO = 15
VENCIMENTO = 22
DIA_VIRADA = 10
CAMPOS = ['id', 'ts', 'data', 'tipo', 'valor', 'conta', 'categoria', 'descricao', 'pagamento', 'fatura', 'origem', 'confianca', 'conta_origem', 'transcricao', 'msg_id', 'mes_ref']
CATEGORIAS = ['iFood', 'iFood Club', 'Marmita (Fitfood)', 'Combustível', 'Mercado/Supermercado', 'Restaurantes/Lanches', 'Transporte/App', 'Saúde/Academia', 'Psicóloga', 'Streaming', 'Serviços online/Tech', 'Educação/Cursos', 'Viagem/Passagens', 'Pix p/ pessoas', 'Outros']
PADRAO_CONTA_B = {'Mercado/Supermercado', 'Combustível', 'Marmita (Fitfood)'}

def hoje() -> date:
    return datetime.now(TZ).date()

def agora() -> datetime:
    return datetime.now(TZ)

def fatura_de(d: date) -> str:
    if d.day <= FECHAMENTO:
        ano, mes = (d.year, d.month)
    else:
        ano, mes = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return f'{ano:04d}-{mes:02d}-{VENCIMENTO:02d}'

def fatura_aberta(ref: date | None=None) -> str:
    return fatura_de(ref or hoje())

def fecha_em(ref: date | None=None) -> tuple[date, int]:
    d = ref or hoje()
    if d.day <= FECHAMENTO:
        alvo = d.replace(day=FECHAMENTO)
    else:
        ano, mes = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        alvo = date(ano, mes, FECHAMENTO)
    return (alvo, (alvo - d).days)

def vence_em(ref: date | None=None) -> tuple[date, int]:
    d = ref or hoje()
    if d.day <= VENCIMENTO:
        alvo = d.replace(day=VENCIMENTO)
    else:
        ano, mes = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        alvo = date(ano, mes, VENCIMENTO)
    return (alvo, (alvo - d).days)

def _ler_json(caminho, padrao):
    if not os.path.exists(caminho):
        return padrao
    try:
        with open(caminho, encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        os.replace(caminho, caminho + '.corrompido')
        return padrao

def _gravar_json(caminho, dados):
    tmp = caminho + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, caminho)

def ler_orcamento():
    return _ler_json(ORCAMENTO, {})

def gravar_orcamento(o):
    _gravar_json(ORCAMENTO, o)

def ler_recorrentes():
    return _ler_json(RECORRENTES, {'itens': []})

def gravar_recorrentes(r):
    _gravar_json(RECORRENTES, r)

def ler_semanas():
    return _ler_json(SEMANAS, {'ciclos': []})

def gravar_semanas(s):
    _gravar_json(SEMANAS, s)

def ler_pendencias():
    return _ler_json(PENDENCIAS, {'abertas': []})

def gravar_pendencias(p):
    _gravar_json(PENDENCIAS, p)

def ler_state():
    return _ler_json(STATE, {'offset': 0, 'ultimo_run': None, 'avisos': {}})

def gravar_state(s):
    _gravar_json(STATE, s)

def ler_fila():
    return _ler_json(FILA, {'pendentes': []})

def gravar_fila(f):
    _gravar_json(FILA, f)

def ler_lancamentos() -> list[dict]:
    if not os.path.exists(LANCAMENTOS):
        return []
    with open(LANCAMENTOS, encoding='utf-8') as f:
        linhas = list(csv.DictReader(f))
    boas = []
    for l in linhas:
        try:
            l['valor'] = float(l['valor'] or 0)
        except (TypeError, ValueError):
            continue
        for c in CAMPOS:
            l.setdefault(c, '')
        if not l.get('mes_ref'):
            l['mes_ref'] = ciclo_de(l['data'])
        boas.append(l)
    return boas

def gravar_lancamentos(linhas: list[dict]) -> None:
    linhas = sorted(linhas, key=lambda l: (l['data'], l['id']))
    tmp = LANCAMENTOS + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        w.writeheader()
        for l in linhas:
            w.writerow({c: l.get(c, '') for c in CAMPOS})
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LANCAMENTOS)

def proximo_id(linhas: list[dict]) -> int:
    return max((int(l['id']) for l in linhas if str(l['id']).isdigit()), default=0) + 1

def novo_lancamento(linhas: list[dict], **campos) -> dict:
    d = campos.get('data') or hoje().isoformat()
    categoria = campos.get('categoria') or 'Outros'
    pagamento = campos.get('pagamento') or 'nao_informado'
    conta = campos.get('conta')
    conta_origem = campos.get('conta_origem') or 'padrao_categoria'
    if not conta:
        conta = 'B' if categoria in PADRAO_CONTA_B else 'A'
        conta_origem = 'padrao_categoria'
    fatura = fatura_de(date.fromisoformat(d)) if pagamento == 'credito' else ''
    mes_ref = campos.get('mes_ref')
    if not mes_ref:
        mes_ref = ciclo_de(d)
    return {'id': str(campos.get('id') or proximo_id(linhas)), 'ts': campos.get('ts') or agora().isoformat(timespec='seconds'), 'data': d, 'tipo': campos.get('tipo') or 'gasto', 'valor': round(float(campos.get('valor') or 0), 2), 'conta': conta, 'categoria': categoria, 'descricao': campos.get('descricao') or '', 'pagamento': pagamento, 'fatura': fatura, 'origem': campos.get('origem') or 'telegram', 'confianca': campos.get('confianca') or 'alta', 'conta_origem': conta_origem, 'transcricao': campos.get('transcricao') or '', 'msg_id': str(campos.get('msg_id') or ''), 'mes_ref': mes_ref}

def registrar_revisao(motivo: str, transcricao: str) -> None:
    novo = not os.path.exists(REVISAR)
    with open(REVISAR, 'a', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        if novo:
            w.writerow(['ts', 'motivo', 'transcricao'])
        w.writerow([agora().isoformat(timespec='seconds'), motivo, transcricao])

def ciclo_aberto(semanas=None):
    semanas = semanas if semanas is not None else ler_semanas()
    ciclos = [c for c in semanas.get('ciclos', []) if not c.get('fechado_em')]
    return ciclos[-1] if ciclos else None

def periodo_aberto(linhas: list[dict] | None=None, semanas=None) -> dict:
    aberto = ciclo_aberto(semanas)
    if aberto:
        return aberto
    gastos_b = [l for l in linhas or [] if l['conta'] == 'B' and l['tipo'] == 'gasto']
    return {'inicio': min((l['data'] for l in gastos_b), default=hoje().isoformat()), 'desde_id': 0}

def fechar_periodo(valor: float, quando: str | None=None, linhas: list[dict] | None=None) -> dict:
    quando = quando or hoje().isoformat()
    linhas = linhas or []
    semanas = ler_semanas()
    semanas.setdefault('ciclos', [])
    semanas.setdefault('acumulado', 0.0)
    aberto = ciclo_aberto(semanas)
    if aberto is None:
        aberto = periodo_aberto(linhas, semanas)
        semanas['ciclos'].append(aberto)
    gasto = gasto_no_ciclo(linhas, aberto)
    sobra = round(float(valor) - gasto, 2)
    aberto.update({'fechado_em': quando, 'recebido': round(float(valor), 2), 'gasto': round(gasto, 2), 'sobra': sobra})
    semanas['acumulado'] = round(semanas.get('acumulado', 0.0) + sobra, 2)
    corte = max((int(l['id']) for l in linhas if str(l['id']).isdigit()), default=0)
    semanas['ciclos'].append({'inicio': quando, 'desde_id': corte})
    gravar_semanas(semanas)
    return {'fechado': aberto, 'acumulado': semanas['acumulado']}

def acumulado() -> float:
    return round(float(ler_semanas().get('acumulado') or 0), 2)

def categorias_do_periodo(linhas: list[dict], ciclo: dict) -> dict[str, float]:
    if not ciclo or ciclo.get('fechado_em'):
        return {}
    corte = ciclo.get('desde_id', 0)
    fora = {}
    for l in linhas:
        if l['conta'] == 'B' and l['tipo'] == 'gasto' and str(l['id']).isdigit() and (int(l['id']) > corte):
            fora[l['categoria']] = round(fora.get(l['categoria'], 0) + l['valor'], 2)
    return dict(sorted(fora.items(), key=lambda kv: -kv[1]))

def gasto_no_ciclo(linhas: list[dict], ciclo: dict) -> float:
    if not ciclo:
        return 0.0
    if ciclo.get('fechado_em'):
        return float(ciclo.get('gasto') or ciclo.get('gasto_final') or 0)
    corte = ciclo.get('desde_id', 0)
    return sum((l['valor'] for l in linhas if l['conta'] == 'B' and l['tipo'] == 'gasto' and str(l['id']).isdigit() and (int(l['id']) > corte)))

def ciclo_de(d_str: str) -> str:
    try:
        d = date.fromisoformat(d_str[:10])
    except ValueError:
        return d_str[:7]
    if d.day >= DIA_VIRADA:
        return f'{d.year:04d}-{d.month:02d}'
    if d.month == 1:
        return f'{d.year - 1:04d}-12'
    return f'{d.year:04d}-{d.month - 1:02d}'

def mes_aberto_a() -> str:
    return ciclo_de(hoje().isoformat())

def proximo_mes(mes: str) -> str:
    ano, m = (int(mes[:4]), int(mes[5:7]))
    return f'{ano + 1:04d}-01' if m == 12 else f'{ano:04d}-{m + 1:02d}'

def mes_anterior(mes: str) -> str:
    ano, m = (int(mes[:4]), int(mes[5:7]))
    return f'{ano - 1:04d}-12' if m == 1 else f'{ano:04d}-{m - 1:02d}'

def mes_de(l: dict | str) -> str:
    if isinstance(l, dict):
        return l.get('mes_ref') or ciclo_de(l['data'])
    return l[:7]

def gastos_do_mes(linhas: list[dict], mes: str, conta: str='A') -> float:
    return sum((l['valor'] for l in linhas if l['conta'] == conta and l['tipo'] == 'gasto' and (mes_de(l) == mes)))

def receitas_do_mes(linhas: list[dict], mes: str, conta: str='A') -> float:
    return sum((l['valor'] for l in linhas if l['conta'] == conta and l['tipo'] == 'receita' and (mes_de(l) == mes)))

def por_categoria(linhas: list[dict], mes: str, conta: str='A') -> dict[str, float]:
    fora = {}
    for l in linhas:
        if l['conta'] != conta or l['tipo'] != 'gasto' or mes_de(l) != mes:
            continue
        fora[l['categoria']] = round(fora.get(l['categoria'], 0) + l['valor'], 2)
    return dict(sorted(fora.items(), key=lambda kv: -kv[1]))

def total_fatura(linhas: list[dict], fatura: str) -> float:
    return sum((l['valor'] for l in linhas if l.get('fatura') == fatura and l['tipo'] == 'gasto'))

def esperado_por_categoria(orcamento: dict) -> dict[str, float]:
    fora = {}
    for item in orcamento.get('A', {}).get('itens', []):
        c = item['categoria']
        fora[c] = round(fora.get(c, 0) + float(item['esperado']), 2)
    for c, v in orcamento.get('A', {}).get('overrides', {}).items():
        fora[c] = round(float(v), 2)
    return fora

def definir_esperado(orcamento: dict, categoria: str, valor: float) -> None:
    itens = [i for i in orcamento.get('A', {}).get('itens', []) if i['categoria'] == categoria]
    if len(itens) == 1:
        itens[0]['esperado'] = round(float(valor), 2)
        orcamento['A'].get('overrides', {}).pop(categoria, None)
    else:
        orcamento.setdefault('A', {}).setdefault('overrides', {})[categoria] = round(float(valor), 2)

def num(v: float) -> str:
    s = f'{abs(v):,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')
    return f'-{s}' if v < 0 else s

def brl(v: float) -> str:
    return ('-R$ ' if v < 0 else 'R$ ') + num(v).lstrip('-')
CURTO = {'Restaurantes/Lanches': 'Comer fora', 'Mercado/Supermercado': 'Mercado', 'Marmita (Fitfood)': 'Marmita', 'Transporte/App': 'Transporte', 'Saúde/Academia': 'Saúde', 'Serviços online/Tech': 'Tech', 'Educação/Cursos': 'Educação', 'Viagem/Passagens': 'Viagem', 'Pix p/ pessoas': 'Pix', 'Combustível': 'Combust.'}

def curto(categoria: str) -> str:
    return CURTO.get(categoria, categoria)

def semana_passou(ciclo: dict, dias: int=8) -> bool:
    if not ciclo:
        return True
    return (hoje() - date.fromisoformat(ciclo['inicio'])).days >= dias