import argparse
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

PORTUGUESE_STOPWORDS = {
    'a', 'o', 'e', 'de', 'da', 'do', 'das', 'dos', 'para', 'com', 'sem', 'em', 'no', 'na',
    'nos', 'nas', 'um', 'uma', 'uns', 'umas', 'por', 'que', 'como', 'ao', 'aos', 'à', 'às',
    'ou', 'se', 'sua', 'seu', 'suas', 'seus', 'meu', 'minha', 'meus', 'minhas', 'the', 'and',
    'of', 'to', 'in', 'for', 'on', 'with', 'at', 'an', 'as', 'is', 'are', 'be', 'this', 'that',
    'from', 'by', 'or', 'it', 'you', 'your', 'will', 'have', 'has', 'using', 'use',
    'estamos', 'buscando', 'procura', 'procurando', 'vaga', 'oportunidade', 'pessoa', 'profissional',
    'area', 'time', 'empresa', 'para', 'atuar', 'como', 'cargo', 'posição', 'posicao', 'role', 'job'
}

SECTION_PATTERNS = {
    'resumo': [r'\bresumo\b', r'\bsummary\b', r'\bperfil\b'],
    'experiencia': [r'\bexperiencia\b', r'\bexperience\b'],
    'habilidades': [r'\bhabilidades\b', r'\bcompetencias\b', r'\bskills\b'],
    'formacao': [r'\bformacao\b', r'\beducacao\b', r'\beducation\b'],
}

ATS_RED_FLAGS = {
    'muitas tabelas ou separadores visuais': r'[|]{2,}|[═—–_-]{4,}',
    'excesso de caracteres decorativos': r'[★•◆■▶◉✓✔❖❯❱❮❬]',
    'muitos elementos em CAIXA ALTA': r'(?:\b[A-Z]{4,}\b.*){8,}',
}


def strip_accents(value: str) -> str:
    return ''.join(
        char for char in unicodedata.normalize('NFKD', value)
        if not unicodedata.combining(char)
    )


def normalize_text(value: str) -> str:
    value = strip_accents(value.lower())
    value = re.sub(r'[^a-z0-9\s+#/-]', ' ', value)
    return re.sub(r'\s+', ' ', value).strip()


def tokenize(value: str) -> List[str]:
    normalized = normalize_text(value)
    tokens = re.findall(r'[a-z0-9+#/-]{2,}', normalized)
    return [token for token in tokens if token not in PORTUGUESE_STOPWORDS and not token.isdigit()]


def load_text(path: str) -> str:
    return Path(path).read_text(encoding='utf-8')


def extract_keywords(job_description: str, limit: int) -> List[Tuple[str, int]]:
    counts = Counter(tokenize(job_description))
    ranked = []
    for token, freq in counts.most_common():
        if len(token) < 3:
            continue
        if token.count('/') > 1:
            continue
        ranked.append((token, freq))
        if len(ranked) >= limit:
            break
    return ranked


def keyword_alignment(resume_text: str, job_description: str, limit: int = 25) -> Dict[str, object]:
    resume_tokens = set(tokenize(resume_text))
    target_keywords = extract_keywords(job_description, limit)
    matched = []
    missing = []

    for token, freq in target_keywords:
        item = {'keyword': token, 'weight': freq}
        if token in resume_tokens:
            matched.append(item)
        else:
            missing.append(item)

    total_weight = sum(weight for _, weight in target_keywords) or 1
    match_weight = sum(item['weight'] for item in matched)
    score = round((match_weight / total_weight) * 100, 1)

    return {
        'score': score,
        'matched_keywords': matched,
        'missing_keywords': missing,
        'target_keywords': target_keywords,
    }


def evaluate_structure(resume_text: str) -> Dict[str, object]:
    normalized = normalize_text(resume_text)
    found_sections = {}
    score = 0

    for section, patterns in SECTION_PATTERNS.items():
        found = any(re.search(pattern, normalized) for pattern in patterns)
        found_sections[section] = found
        score += 7 if found else 0

    email_found = bool(re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', resume_text))
    phone_found = bool(re.search(r'(?:\+?\d{1,3}\s*)?(?:\(?\d{2}\)?\s*)?\d{4,5}[-\s]?\d{4}', resume_text))
    linkedin_found = 'linkedin.com/' in resume_text.lower()
    score += 6 if email_found else 0
    score += 6 if phone_found else 0
    score += 6 if linkedin_found else 0

    bullets = len(re.findall(r'^[\-•*]\s+', resume_text, flags=re.MULTILINE))
    quantified_bullets = len(re.findall(r'^[\-•*].*\b\d+(?:[%.,]\d+)?\b', resume_text, flags=re.MULTILINE))
    score += min(bullets, 8)
    score += min(quantified_bullets * 2, 10)

    return {
        'score': min(score, 50),
        'sections': found_sections,
        'contact': {
            'email': email_found,
            'phone': phone_found,
            'linkedin': linkedin_found,
        },
        'bullet_points': bullets,
        'quantified_bullets': quantified_bullets,
    }


def detect_red_flags(resume_text: str) -> List[str]:
    flags = []
    for label, pattern in ATS_RED_FLAGS.items():
        if re.search(pattern, resume_text):
            flags.append(label)

    lines = [line.strip() for line in resume_text.splitlines() if line.strip()]
    long_lines = [line for line in lines if len(line) > 140]
    if len(long_lines) >= 5:
        flags.append('muitas linhas longas; pode prejudicar legibilidade no parser ATS')

    if '.png' in resume_text.lower() or '.jpg' in resume_text.lower() or 'canva' in resume_text.lower():
        flags.append('possível dependência de elementos visuais/imagens no currículo')

    return flags


def overall_score(keyword_score: float, structure_score: float, red_flags: List[str]) -> float:
    raw = keyword_score * 0.7 + structure_score * 0.3
    penalty = min(len(red_flags) * 4, 12)
    return round(max(raw - penalty, 0), 1)


def make_recommendations(alignment: Dict[str, object], structure: Dict[str, object], red_flags: List[str]) -> List[str]:
    recommendations = []

    missing = alignment['missing_keywords'][:8]
    if missing:
        recommendations.append(
            'Inclua palavras-chave da vaga de forma natural: ' + ', '.join(item['keyword'] for item in missing[:5]) + '.'
        )

    sections = structure['sections']
    missing_sections = [name for name, found in sections.items() if not found]
    if missing_sections:
        recommendations.append(
            'Adicione seções explícitas para ATS reconhecer melhor: ' + ', '.join(missing_sections) + '.'
        )

    if structure['quantified_bullets'] < 3:
        recommendations.append('Transforme experiências em bullets com métricas, por exemplo: “aumentei conversão em 18%”.')

    contact = structure['contact']
    if not all(contact.values()):
        missing_contact = [label for label, found in contact.items() if not found]
        recommendations.append('Complete os dados de contato: ' + ', '.join(missing_contact) + '.')

    if red_flags:
        recommendations.append('Simplifique o layout para ATS, removendo elementos como tabelas, ícones ou excesso de decoração.')

    if not recommendations:
        recommendations.append('Seu currículo está bem alinhado para ATS. Foque agora em adaptar exemplos e resultados para cada vaga.')

    return recommendations


def evaluate_resume(resume_text: str, job_description: str) -> Dict[str, object]:
    alignment = keyword_alignment(resume_text, job_description)
    structure = evaluate_structure(resume_text)
    red_flags = detect_red_flags(resume_text)
    final_score = overall_score(alignment['score'], structure['score'], red_flags)

    return {
        'ats_score': final_score,
        'keyword_alignment': alignment,
        'structure': structure,
        'red_flags': red_flags,
        'recommendations': make_recommendations(alignment, structure, red_flags),
    }


def rating_label(score: float) -> str:
    if score >= 85:
        return 'Excelente'
    if score >= 70:
        return 'Bom'
    if score >= 50:
        return 'Mediano'
    return 'Baixo'


def render_report(result: Dict[str, object]) -> str:
    alignment = result['keyword_alignment']
    structure = result['structure']
    lines = [
        f"ATS Score: {result['ats_score']}/100 ({rating_label(result['ats_score'])})",
        f"Aderência a palavras-chave: {alignment['score']}/100",
        f"Estrutura: {structure['score']}/50",
        '',
        'Palavras-chave encontradas:',
    ]

    matched = alignment['matched_keywords'][:10]
    missing = alignment['missing_keywords'][:10]

    lines.extend(f"- {item['keyword']} (peso {item['weight']})" for item in matched) if matched else lines.append('- nenhuma')
    lines.append('')
    lines.append('Palavras-chave ausentes:')
    lines.extend(f"- {item['keyword']} (peso {item['weight']})" for item in missing) if missing else lines.append('- nenhuma')
    lines.append('')
    lines.append('Checagem estrutural:')
    for section, found in structure['sections'].items():
        status = 'OK' if found else 'FALTA'
        lines.append(f'- {section}: {status}')
    for label, found in structure['contact'].items():
        status = 'OK' if found else 'FALTA'
        lines.append(f'- contato {label}: {status}')
    lines.append(f"- bullet points: {structure['bullet_points']}")
    lines.append(f"- bullets com métricas: {structure['quantified_bullets']}")
    lines.append('')

    if result['red_flags']:
        lines.append('Alertas ATS:')
        lines.extend(f'- {flag}' for flag in result['red_flags'])
        lines.append('')

    lines.append('Recomendações:')
    lines.extend(f'- {recommendation}' for recommendation in result['recommendations'])
    return '\n'.join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Avalia currículo em texto usando heurísticas de ATS e aderência à vaga.'
    )
    parser.add_argument('resume', help='Arquivo .txt ou .md com o currículo exportado em texto')
    parser.add_argument('job', help='Arquivo .txt ou .md com a descrição da vaga')
    parser.add_argument('--json', action='store_true', dest='as_json', help='Retorna o resultado em JSON')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    resume_text = load_text(args.resume)
    job_description = load_text(args.job)
    result = evaluate_resume(resume_text, job_description)

    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_report(result))


if __name__ == '__main__':
    main()
