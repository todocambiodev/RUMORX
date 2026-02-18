import json, asyncio, re, argparse, requests
from pathlib import Path
from typing import Optional, Dict, Any
from playwright.async_api import async_playwright, Page

# =================================================================
# CONFIGURATION & CONSTANTS
# =================================================================
STORAGE_STATE_PATH = Path("storage_state.json")
GROK_URL = "https://x.com/i/grok?conversation=2024113740904362311"

# Modern Stealth User-Agent
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
VIEWPORT = {"width": 1280, "height": 800}

PROMPT_TEMPLATE = """Puedes buscar rumores dentro de X (de usuarios verificados, reconocidos, de credibilidad alta y de relevancia) de último minuto que puedan mover los mercados financieros de los futuros del Oro (GC1!), DXY (DX1!), SP500 (ES1!) y BTC (DEL CME) el día de HOY?
Identifica los o el más relevantes y respóndeme únicamente con un JSON estructurado de la siguiente manera:

{
  "gold": {
    "titulo": "Titulo del rumor o noticia que mueva los futuros del oro",
    "razon": "Rumor o noticia que mueva los futuros del oro",
    "nivel": "Nivel de la noticia/rumor (CRITICA/NORMAL/NEUTRAL)",
    "precio": "Precio del oro justo en este momento",
    "sentimiento": "BUY/SELL/NEUTRAL"
  },
  "dxy": {
    "titulo": "Titulo del rumor o noticia que mueva los futuros del dxy",
    "razon": "Rumor o noticia que mueva los futuros del dxy",
    "nivel": "Nivel de la noticia/rumor (CRITICA/NORMAL/NEUTRAL)",
    "precio": "Precio del dxy justo en este momento",
    "sentimiento": "BUY/SELL/NEUTRAL"
  },
  "sp500": {
    "titulo": "Titulo del rumor o noticia que mueva los futuros del SP500",
    "razon": "Rumor o noticia que mueva los futuros del SP500",
    "nivel": "Nivel de la noticia/rumor (CRITICA/NORMAL/NEUTRAL)",
    "precio": "Precio del SP500 justo en este momento",
    "sentimiento": "BUY/SELL/NEUTRAL"
  },
  "btc": {
    "titulo": "Titulo del rumor o noticia que mueva los futuros del BTC",
    "razon": "Rumor o noticia que mueva los futuros del BTC",
    "nivel": "Nivel de la noticia/rumor (CRITICA/NORMAL/NEUTRAL)",
    "precio": "Precio del BTC justo en este momento",
    "sentimiento": "BUY/SELL/NEUTRAL"
  }
}
Traduce al español los campos titulo y razon.
"""
URL_GOLD = "https://script.google.com/macros/s/AKfycbyJyyN7WFPtao1u_y8jgwsaKVYf2j8TL4vtg-Xe3kAotmBsUAEyFFjt2K-NgHauYxJjHw/exec"
URL_SP500 = "https://script.google.com/macros/s/AKfycbz66LZjqBsdyZGCFRJ6Ove4_FdHJrOAhaWEsmlucAn8r9Jsph-Nmo9PzMlAsK-LG9qAHg/exec"
URL_BTC = ""
# =================================================================
# UTILITIES
# =================================================================

def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Limpia y extrae el contenido JSON de una respuesta de texto."""
    try:
        # Busca el bloque JSON usando balanceo de llaves básico
        match = re.search(r'(\{.*\})', text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        return None
    except json.JSONDecodeError:
        return None

async def wait_for_grok_response(page: Page, timeout_ms: int = 900000) -> str:
    """Espera de forma inteligente a que Grok termine de escribir la respuesta."""
    print("[*] Esperando respuesta de Grok (streaming)...")
    
    last_len = 0
    stable_iterations = 0
    start_time = asyncio.get_event_loop().time()
    
    while (asyncio.get_event_loop().time() - start_time) < (timeout_ms / 1000):
        # Buscamos todos los posibles contenedores de mensajes
        elements = await page.query_selector_all("div[dir='ltr']")
        
        # Buscamos el elemento más reciente que parezca contener nuestra respuesta JSON
        # Iteramos desde el final hacia el principio para encontrar el mensaje actual
        target_text = ""
        for element in reversed(elements):
            text = (await element.inner_text()).strip()
            # Flexibilidad: Debe contener { y nuestras claves. Ya no exigimos que EMPIECE por {
            # Esto permite capturar bloques que digan "json\n{" o similares.
            if "{" in text and ("gold" in text or "sp500" in text or "btc" in text) and "BUY/SELL/NEUTRAL" not in text:
                target_text = text
                break
        
        if not target_text:
            await asyncio.sleep(1)
            continue
            
        current_len = len(target_text)
        print(f"[*] Detectado posible JSON (longitud: {current_len})...", end="\r")
        
        # Si el texto es sustancial y no ha cambiado en 4 iteraciones (2s), asumimos fin
        if current_len > 100 and current_len == last_len:
            stable_iterations += 1
            if stable_iterations >= 4:
                print(f"\n[+] Respuesta completada ({current_len} caracteres).")
                await page.close()
                return target_text
        else:
            stable_iterations = 0
            last_len = current_len
            
        await asyncio.sleep(0.5)
        
    raise TimeoutError("Grok tardó demasiado en responder.")

def enviar_noticia_a_gsheets(modelo:str, url:str, noticia:dict):
    try:
        # Preparamos los parámetros para el envío vía POST/GET
        params = {
            "noticia": noticia,
            "titular": noticia["titulo"], 
            "descripcion": noticia["razon"], 
            "sentimiento": noticia["sentimiento"],
            "precio_actual": noticia["precio"],
            "nivel": noticia["nivel"],
            "modelo": modelo
        }
        print(f"Enviando titular: {params['titular']}")
        
        # Realizamos la petición HTTP al script de Google
        r = requests.post(url=url, params=params)
        
        if r.status_code == 200:
            print(f"Respuesta GSheets: {r.text}")
        else:
            print(f"ERROR ENVIANDO NOTICIAS AL GSHEETS. STATUS CODE: {r.status_code}")
            
    except Exception as e:
        print(f"ERROR EN enviar_noticia_a_gsheets() - ERROR: {e} NOTICIA: {noticia}")

# =================================================================
# BROWSER ENGINE
# =================================================================

async def get_context(playwright, headful: bool = False) -> tuple:
    """Configura el navegador con parámetros de evasión y persistencia."""
    browser = await playwright.chromium.launch(
        headless=not headful,
        args=["--disable-blink-features=AutomationControlled"]
    )
    
    context_args = {
        "user_agent": USER_AGENT,
        "viewport": VIEWPORT,
        "locale": "es-ES",
        "timezone_id": "Europe/Madrid",
    }
    
    if STORAGE_STATE_PATH.exists():
        context_args["storage_state"] = str(STORAGE_STATE_PATH)
        print(f"[*] Sesión cargada desde {STORAGE_STATE_PATH}")
    
    context = await browser.new_context(**context_args)
    
    # Scripts de evasión adicionales
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    """)
    
    return browser, context

# =================================================================
# MAIN FLOW
# =================================================================

async def monitor_markets(force_login: bool = False, headful: bool = False):
    async with async_playwright() as p:
        # Decisión de modo: Headful si no hay sesión o se fuerza login
        is_first_run = not STORAGE_STATE_PATH.exists() or force_login
        headful = headful or is_first_run
        
        # Lanzamos el navegador UNA sola vez. 
        # Forzamos modo visible (headful=True) para login o si el usuario quiere ver el proceso.
        # Si prefieres que sea 100% invisible en modo auto, cambia a headful=is_first_run
        browser, context = await get_context(p, headful=headful)
        page = await context.new_page()
        
        if is_first_run:
            print("\n" + "!"*40)
            print(" MODO LOGIN MANUAL ACTIVADO")
            print("!"*40)
            print("1. Inicia sesión en X (Twitter) manualmente.")
            print("2. Recomendado: Usa 'Iniciar sesión con Google'.")
            print("3. Una vez logueado, navega a la sección de Grok.")
            print("4. El script detectará la entrada de Grok y guardará la sesión.")
            
            await page.goto("https://x.com/login")
            
            try:
                # 1. Esperamos a que el usuario llegue al Home o cualquier página que indique login
                print("[*] Verificando sesión (detectando navegación al Home)...")
                await page.wait_for_selector("[data-testid='SideNav_NewTweet_Button']", timeout=60000)
                print("[+] Login verificado.")
                
                # 2. Navegamos automáticamente a Grok
                print("[*] Navegando automáticamente a la interfaz de Grok...")
                await page.goto(GROK_URL)
                
                # 3. Esperamos a que Grok esté listo
                print("[*] Esperando interfaz de Grok...")
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(3)
                
                # 4. Guardamos la sesión
                await context.storage_state(path=str(STORAGE_STATE_PATH))
                print(f"[+] Sesión guardada en {STORAGE_STATE_PATH}")
                
            except Exception as e:
                print(f"[*] Siguiendo flujo (el error fue: {e})")
                pass
        else:
            # Modo Automatizado: Ya tenemos el browser y la page listos
            print("[*] Iniciando monitoreo automatizado...")
            await page.goto(GROK_URL, wait_until="domcontentloaded")
        
        try:
            # Selectores verificados por investigación dinámica
            input_selector = 'textarea[placeholder="Ask anything"]'
            send_button_selector = 'button[aria-label="Grok something"]'
            
            await page.wait_for_load_state("load")
            await asyncio.sleep(2)
            
            # Esperar al input visible
            print(f"[*] Localizando campo de entrada...")
            await page.wait_for_selector(input_selector, timeout=63000)
            
            print(f"[*] Introduciendo prompt en Grok...")
            await page.click(input_selector)
            await page.fill(input_selector, PROMPT_TEMPLATE)
            await asyncio.sleep(1)
            
            # Intentar click en el botón de enviar
            try:
                await page.click(send_button_selector, timeout=5000)
                print("[*] Botón 'Grok something' clickeado.")
            except:
                print("[*] Botón no detectado, intentando Enter...")
                await page.keyboard.press("Enter")
            
            # Obtener respuesta
            raw_response = await wait_for_grok_response(page)
            data = extract_json(raw_response)
            
            if data:
                print("\n" + "═"*50)
                print(" MARKET ANALYSIS JSON ")
                print("═"*50)
                print(json.dumps(data, indent=2, ensure_ascii=False))
                print("═"*50 + "\n")
                enviar_noticia_a_gsheets(modelo="Grok 4", url=URL_GOLD, noticia=data["gold"])
                enviar_noticia_a_gsheets(modelo="Grok 4", url=URL_SP500, noticia=data["sp500"])
                #enviar_noticia_a_gsheets(modelo="Grok 4", url=URL_BTC, noticia=data["btc"])
            else:
                print("[-] No se pudo extraer JSON. Respuesta cruda:")
                print(raw_response)
                
        except Exception as e:
            print(f"[-] Error en el flujo de Grok: {e}")
            # Si falla por sesión, sugerimos borrar el estado
            print("[!] Tip: Si es un error de sesión, borra 'storage_state.json' y usa --login")

        await browser.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Grok Market Monitor Automation")
    parser.add_argument("--login", action="store_true", help="Forzar modo login visible")
    parser.add_argument("--headful", action="store_true", help="Forzar modo visible")
    args = parser.parse_args()

    try:
        asyncio.run(monitor_markets(force_login=args.login, headful=args.headful))
    except KeyboardInterrupt:
        print("\n[!] Abortado por el usuario.")

