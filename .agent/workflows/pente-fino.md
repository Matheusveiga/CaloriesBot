---
description: Rotina de Pente Fino (Code Review & Stability Check)
---

Este workflow deve ser executado antes de qualquer entrega de código para garantir estabilidade e evitar quebras no deploy.

// turbo-all

1. **Validação de Sintaxe (Python):**
   Execute o compilador do Python em modo check para encontrar erros de indentação ou sintaxe.
   ```powershell
   python -m py_compile [CAMINHO_DO_ARQUIVO]
   ```

2. **Verificação de Importações:**
   Garanta que todas as novas bibliotecas usadas estejam no `requirements.txt` e importadas no topo do arquivo.

3. **Consistência Async/Await:**
   - Verifique se todas as chamadas `async` possuem um `await`.
   - Garanta que clientes (Gemini, Groq, Supabase) estejam usando as versões assíncronas quando dentro de handlers.

4. **Escopo de Variáveis:**
   Verifique se variáveis usadas dentro de blocos `try/except` ou `if/else` estão devidamente inicializadas ou passadas como argumentos (ex: `state`, `message`, `user_id`).

5. **Tratamento de Erros:**
   Certifique-se de que os handlers principais (como `handle_text` e `handle_photo`) possuem um try/except global para evitar loops de webhook em caso de crash.

6. **Limpeza de Debug:**
   Remova scripts temporários em `/tmp/` ou prints de debug desnecessários.
