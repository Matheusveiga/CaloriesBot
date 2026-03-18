# CaloriesBot 🍏🤖

O **CaloriesBot** é um assistente de nutrição de elite para Telegram, focado em precisão cirúrgica e experiência de usuário premium.

## 🏛️ Arquitetura Geral

- **Core:** [main.py](main.py) (Monolito para performance máxima no Render).
- **Backend:** FastAPI + Python 3.10+.
- **Banco de Dados:** Supabase (PostgreSQL + pgvector).
- **IA Engine:** Groq (Llama 3.3 70B para texto) + Gemini 2.0 Flash (Visão).

## ♟️ Estratégia "Checkmate" (Busca de Alimentos)

Para garantir 100% de acerto nas calorias, o bot segue este fluxo:
1. **Universal Catalog:** Busca vetorial (768d) por itens já validados.
2. **Global FatSecret:** Busca no banco de dados mundial (sem travas de região/idioma).
   - **Hierarquia:** Inglês Genérico -> Inglês Específico -> Português.
   - **70B Judge:** Um modelo de 70B seleciona o melhor match ou rejeita "lixo".
3. **Escalonamento:** A IA escala os macros da porção lida (ex: 1 fatia = 26g) para a quantidade do usuário (ex: 2 fatias = 52g).

## 🎨 UX Premium (Master Touch)

- **Relatórios:** Peso humanizado: `2 fatias (52g)` em vez de apenas gramas.
- **Feedback Loop:** Teclado inline com ajuste de ±10%, correção manual e desfazer.
- **Segurança:** Proteção contra Jailbreak com respostas sarcásticas.

## 🛠️ Manutenção (Sessão 18/03)

- **Sem .env:** Todas as chaves estão no **Render Dashboard**.
- **Proxies:** Rotação de proxies Webshare via `httpx.AsyncClient(proxy=...)`.
- **Handoff:** Consulte a pasta `.agent/skills/` para detalhes técnicos de cada componente.

---
*Documentado em 18 de Março de 2026.* 🍏🚀
