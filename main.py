from flask import Flask, request, jsonify
from zeep import Client
from zeep.transports import Transport
from zeep.exceptions import Fault
from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
import datetime
import os
import base64
import subprocess
import ssl

app = Flask(__name__)

# Configuración SSL para permitir conexión a AFIP
class DESAdapter(HTTPAdapter):
    """
    Adaptador que permite cifrados DH antiguos para conectarse a AFIP
    """
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context()
        context.set_ciphers('DEFAULT@SECLEVEL=1')
        kwargs['ssl_context'] = context
        return super(DESAdapter, self).init_poolmanager(*args, **kwargs)
    
    def proxy_manager_for(self, *args, **kwargs):
        context = create_urllib3_context()
        context.set_ciphers('DEFAULT@SECLEVEL=1')
        kwargs['ssl_context'] = context
        return super(DESAdapter, self).proxy_manager_for(*args, **kwargs)

# ----------------------------------------------------------------------
# CONFIGURACIÓN CUITS Y CERTIFICADOS
# ----------------------------------------------------------------------

CUIT_1 = "27239676931"
CUIT_2 = "27461124149"

CERT_1 = "facturacion27239676931.crt"
KEY_1  = "cuit_27239676931.key"

CERT_2 = "facturacion27461124149.crt"
KEY_2  = "cuit_27461124149.key"

# Datos de los emisores
EMISOR_DATA = {
    "27239676931": {
        "razon_social": "DEVRIES MARIA PAULA",
        "domicilio": "Rodriguez Peña 1789 - Mar Del Plata Sur, Buenos Aires",
        "ingresos_brutos": "27239676931",
        "inicio_actividades": "01/01/2021"
    },
    "27461124149": {
        "razon_social": "CACCIATO MARIA MERCEDES",
        "domicilio": "General Paz 4662 - Mar Del Plata Sur, Buenos Aires",
        "ingresos_brutos": "27461124149",
        "inicio_actividades": "01/12/2023"
    }
}

# ----------------------------------------------------------------------
# AFIP ENDPOINTS
# ----------------------------------------------------------------------

WSAA = "https://wsaa.afip.gov.ar/ws/services/LoginCms?wsdl"
WSFE = "https://servicios1.afip.gov.ar/wsfev1/service.asmx?WSDL"

# ----------------------------------------------------------------------
# UTILIDADES
# ----------------------------------------------------------------------

def load_cert(cuit_emisor):
    if cuit_emisor == CUIT_1:
        return CERT_1, KEY_1
    elif cuit_emisor == CUIT_2:
        return CERT_2, KEY_2
    else:
        raise Exception("CUIT emisor sin certificado configurado")

def create_cms(tra_path, cert_file, key_file):
    cmd_list = [
        "openssl", "cms", "-sign", "-in", tra_path,
        "-signer", cert_file, "-inkey", key_file,
        "-nodetach", "-outform", "der"
    ]
    
    try:
        result = subprocess.run(cmd_list, capture_output=True, check=True)
        cms_bin = result.stdout
    except subprocess.CalledProcessError as e:
        error_message = e.stderr.decode('utf-8', errors='ignore')
        raise Exception(f"Error OpenSSL: {error_message}")

    cms_b64 = base64.b64encode(cms_bin).decode("ascii")
    return cms_b64

def get_token_sign(cert_file, key_file):
    now = datetime.datetime.utcnow()
    
    generation_time = now.strftime('%Y-%m-%dT%H:%M:%S.000-00:00')
    expiration_time = (now + datetime.timedelta(hours=12)).strftime('%Y-%m-%dT%H:%M:%S.000-00:00')

    tra = f"""<?xml version="1.0" encoding="UTF-8"?>
<loginTicketRequest version="1.0">
  <header>
    <uniqueId>{int(now.timestamp())}</uniqueId>
    <generationTime>{generation_time}</generationTime>
    <expirationTime>{expiration_time}</expirationTime>
  </header>
  <service>wsfe</service>
</loginTicketRequest>"""

    tra_path = os.path.join(os.getcwd(), "tra.xml")
    
    with open(tra_path, "w") as f:
        f.write(tra)

    cms = create_cms(tra_path, cert_file, key_file)
    
    os.remove(tra_path)

    # Usar sesión con adaptador SSL personalizado
    session = Session()
    session.mount('https://', DESAdapter())
    transport = Transport(session=session)
    client = Client(WSAA, transport=transport)
    
    try:
        response = client.service.loginCms(cms)
        
        # Debug: ver qué tipo de objeto es
        print(f"DEBUG Response type: {type(response)}")
        
        # Intentar diferentes formas de acceder
        try:
            # Forma 1: objeto con atributos
            token = response.credentials.token
            sign = response.credentials.sign
            print("✓ Token y Sign obtenidos exitosamente")
        except AttributeError:
            try:
                # Forma 2: diccionario
                token = response['credentials']['token']
                sign = response['credentials']['sign']
                print("✓ Token y Sign obtenidos exitosamente (dict)")
            except (KeyError, TypeError):
                # Forma 3: buscar en el XML directamente
                import xml.etree.ElementTree as ET
                
                # Convertir a string si es necesario
                response_str = str(response)
                
                # Parsear XML
                root = ET.fromstring(response_str)
                
                token = None
                sign = None
                
                for elem in root.iter():
                    tag_lower = elem.tag.lower()
                    if 'token' in tag_lower and elem.text:
                        token = elem.text
                    if 'sign' in tag_lower and elem.text:
                        sign = elem.text
                
                if not token or not sign:
                    raise Exception(f"No se pudo extraer token/sign. Response: {response_str[:500]}")
                
                print("✓ Token y Sign obtenidos exitosamente (XML)")
        
        return token, sign
        
    except Fault as e:
        raise Exception(f"Error WSAA: {e.message}")
    except Exception as e:
        raise Exception(f"Error token: {str(e)}")

# ----------------------------------------------------------------------
# FACTURACIÓN
# ----------------------------------------------------------------------

def crear_factura(data):
    # Limpiar CUITs de guiones y espacios
    cuit_emisor   = str(data["cuit_emisor"]).replace("-", "").replace(" ", "").strip()
    cuit_receptor = str(data["cuit_receptor"]).replace("-", "").replace(" ", "").strip()
    punto_venta   = int(data["punto_venta"])
    tipo_cbte     = int(data["tipo_cbte"])
    importe       = float(data["importe"])
    
    # Log para debugging
    print(f"\n=== INICIANDO FACTURACIÓN ===")
    print(f"CUIT Emisor: {cuit_emisor}")
    print(f"CUIT Receptor: {cuit_receptor}")
    print(f"Punto Venta: {punto_venta}")
    print(f"Tipo Comprobante: {tipo_cbte}")
    print(f"Importe: {importe}")

    cert_file, key_file = load_cert(cuit_emisor)
    print(f"Certificado: {cert_file}")

    # 1) Token AFIP
    print("\n1. Obteniendo token AFIP...")
    token, sign = get_token_sign(cert_file, key_file)

    # 2) Cliente WSFE con headers y SSL configurado
    print("2. Conectando a WSFE...")
    session = Session()
    session.mount('https://', DESAdapter())
    session.headers.update({
        'Content-Type': 'text/xml; charset=utf-8'
    })
    transport = Transport(session=session)
    client = Client(WSFE, transport=transport)

    # 3) Último comprobante
    print(f"3. Consultando último comprobante (PtoVta: {punto_venta}, Tipo: {tipo_cbte})...")
    try:
        ultimo = client.service.FECompUltimoAutorizado(
            Auth={'Token': token, 'Sign': sign, 'Cuit': int(cuit_emisor)},
            PtoVta=punto_venta,
            CbteTipo=tipo_cbte
        )
        cbte_nro = ultimo.CbteNro + 1
        print(f"✓ Último comprobante: {ultimo.CbteNro}, Próximo: {cbte_nro}")
    except Fault as e:
        print(f"✗ Error en último comprobante: {e.message}")
        raise Exception(f"Error último comprobante: {e.message}")
    except Exception as e:
        print(f"✗ Error en último comprobante: {str(e)}")
        raise Exception(f"Error último comprobante: {str(e)}")

    # 4) Preparar comprobante
    print("4. Preparando comprobante...")
    fecha = int(datetime.datetime.now().strftime("%Y%m%d"))
    
    FeCabReq = {
        'CantReg': 1,
        'PtoVta': punto_venta,
        'CbteTipo': tipo_cbte
    }
    
    FeDetReq = {
        'Concepto': 1,
        'DocTipo': 80,
        'DocNro': int(cuit_receptor),
        'CbteDesde': cbte_nro,
        'CbteHasta': cbte_nro,
        'CbteFch': fecha,
        'ImpTotal': round(importe, 2),
        'ImpTotConc': 0.00,
        'ImpNeto': round(importe, 2),
        'ImpOpEx': 0.00,
        'ImpIVA': 0.00,
        'ImpTrib': 0.00,
        'MonId': 'PES',
        'MonCotiz': 1.00
    }
    
    FeCAEReq = {
        'FeCabReq': FeCabReq,
        'FeDetReq': {'FECAEDetRequest': [FeDetReq]}
    }
    
    print(f"Comprobante preparado:")
    print(f"  - Fecha: {fecha}")
    print(f"  - Número: {cbte_nro}")
    print(f"  - Doc Receptor: {int(cuit_receptor)}")
    print(f"  - Importe Total: {round(importe, 2)}")

    # 5) Solicitar CAE
    print("5. Solicitando CAE a AFIP...")
    try:
        resultado = client.service.FECAESolicitar(
            Auth={'Token': token, 'Sign': sign, 'Cuit': int(cuit_emisor)},
            FeCAEReq=FeCAEReq
        )
        
        print(f"Respuesta AFIP recibida")
        
        # Verificar errores generales
        if hasattr(resultado, 'Errors') and resultado.Errors:
            error_msg = resultado.Errors.Err[0].Msg
            error_code = resultado.Errors.Err[0].Code
            print(f"✗ AFIP devolvió error {error_code}: {error_msg}")
            raise Exception(f"AFIP error {error_code}: {error_msg}")
        
        # Obtener respuesta del detalle
        det_resp = resultado.FeDetResp.FECAEDetResponse[0]
        
        print(f"Resultado detallado:")
        print(f"  - Resultado: {det_resp.Resultado}")
        
        # Verificar observaciones
        if hasattr(det_resp, 'Observaciones') and det_resp.Observaciones:
            for obs in det_resp.Observaciones.Obs:
                print(f"  - Observación {obs.Code}: {obs.Msg}")
        
        # Verificar si hay CAE
        if not det_resp.CAE:
            if hasattr(det_resp, 'Observaciones') and det_resp.Observaciones:
                obs_msg = det_resp.Observaciones.Obs[0].Msg
                obs_code = det_resp.Observaciones.Obs[0].Code
                print(f"✗ AFIP rechazó (Obs {obs_code}): {obs_msg}")
                raise Exception(f"AFIP rechazó (código {obs_code}): {obs_msg}")
            else:
                print(f"✗ AFIP rechazó sin CAE ni observaciones")
                raise Exception("AFIP rechazó sin CAE ni observaciones")
        
        print(f"✓ CAE obtenido: {det_resp.CAE}")
        print(f"✓ Vencimiento: {det_resp.CAEFchVto}")
        print(f"=== FACTURACIÓN EXITOSA ===\n")
        
        return {
            "cbte_nro": cbte_nro,
            "cae": det_resp.CAE,
            "vencimiento": det_resp.CAEFchVto,
        }
        
    except Fault as e:
        print(f"✗ Fault en CAE: {e.message}")
        raise Exception(f"Error CAE (Fault): {e.message}")
    except Exception as e:
        if "AFIP" in str(e):
            raise
        print(f"✗ Exception en CAE: {str(e)}")
        raise Exception(f"Error CAE: {str(e)}")

# ----------------------------------------------------------------------
# ENDPOINTS
# ----------------------------------------------------------------------

@app.route("/facturar", methods=["POST"])
def facturar():
    try:
        data = request.json
        print(f"\n{'='*60}")
        print(f"NUEVA SOLICITUD DE FACTURACIÓN")
        print(f"{'='*60}")
        factura = crear_factura(data)
        return jsonify({"status": "OK", "factura": factura})
    except Exception as e:
        print(f"\n{'='*60}")
        print(f"ERROR EN FACTURACIÓN: {str(e)}")
        print(f"{'='*60}\n")
        return jsonify({"status": "ERROR", "detalle": str(e)})

@app.route("/", methods=["GET"])
def home():
    return "AFIP Microserver v4 - Funcionando correctamente"

@app.route("/test", methods=["GET"])
def test():
    """Endpoint de prueba para verificar configuración"""
    return jsonify({
        "status": "OK",
        "message": "Servidor funcionando correctamente",
        "cuits_configurados": [CUIT_1, CUIT_2],
        "certificados": {
            CUIT_1: {"cert": CERT_1, "key": KEY_1},
            CUIT_2: {"cert": CERT_2, "key": KEY_2}
        },
        "endpoints": {
            "WSAA": WSAA,
            "WSFE": WSFE
        }
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
