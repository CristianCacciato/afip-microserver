from flask import Flask, request, jsonify
from suds.client import Client
from suds import WebFault
import datetime
import os
import base64
import subprocess
import xml.etree.ElementTree as ET


app = Flask(__name__)

# ----------------------------------------------------------------------
# CONFIGURACIÓN CUITS Y CERTIFICADOS
# ----------------------------------------------------------------------

CUIT_1 = "27239676931"
CUIT_2 = "27461124149"

CERT_1 = "facturacion27239676931.crt"
KEY_1  = "cuit_27239676931.key"

CERT_2 = "facturacion27461124149.crt"
KEY_2  = "cuit_27461124149.key"

# ----------------------------------------------------------------------
# AFIP ENDPOINTS
# ----------------------------------------------------------------------

WSAA = "https://wsaa.afip.gov.ar/ws/services/LoginCms?wsdl"

# HOMOLOGACIÓN (Entorno de pruebas)
WSFE = "https://wswhomo.afip.gov.ar/wsfev1/service.asmx?WSDL"

# PRODUCCIÓN (activar cuando esté todo OK)
# WSFE = "https://servicios1.afip.gov.ar/wsfev1/service.asmx?WSDL"

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
        raise Exception(f"Error OpenSSL al firmar: {error_message}. Revise nombres/permisos de los archivos de certificado y clave.")

    cms_b64 = base64.b64encode(cms_bin).decode("ascii")
    return cms_b64


def get_token_sign(cert_file, key_file):
    now = datetime.datetime.utcnow()
    
    generation_time = now.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    expiration_time = (now + datetime.timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%S.000Z')

    tra = f"""<loginTicketRequest version="1.0">
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

    client = Client(WSAA)
    
    try:
        # Llamada al servicio WSAA (Autenticación)
        response = client.service.loginCms(cms)
        
        # CORRECCIÓN: Parsear la respuesta XML manualmente
        # porque suds a veces no parsea bien los namespaces de AFIP
        response_xml = str(response)
        
        # Intentar extraer usando el objeto directamente primero
        try:
            token = response.credentials.token
            sign = response.credentials.sign
        except AttributeError:
            # Si falla, parsear el XML manualmente
            root = ET.fromstring(response_xml)
            
            # Buscar en todos los namespaces posibles
            token = None
            sign = None
            
            for elem in root.iter():
                if 'token' in elem.tag.lower():
                    token = elem.text
                if 'sign' in elem.tag.lower():
                    sign = elem.text
            
            if not token or not sign:
                raise Exception("No se pudo extraer token/sign de la respuesta WSAA")
        
        return token, sign

    except WebFault as e:
        error_msg = e.fault.faultstring
        raise Exception(f"Error WSAA (AFIP): {error_msg}. Revisar configuración de relación de certificado.")
    except Exception as e:
        raise Exception(f"Error al obtener Token: {str(e)}. Intente re-deployar.")


def get_wsfe_client(token, sign, cuit_emisor):
    client = Client(WSFE)
    client.set_options(
        soapheaders={
            "Token": token,
            "Sign": sign,
            "Cuit": cuit_emisor,
        }
    )
    return client


# ----------------------------------------------------------------------
# FACTURACIÓN
# ----------------------------------------------------------------------

def crear_factura(data):
    cuit_emisor   = str(data["cuit_emisor"])
    cuit_receptor = str(data["cuit_receptor"])
    punto_venta   = int(data["punto_venta"])
    tipo_cbte     = int(data["tipo_cbte"])
    importe       = float(data["importe"])

    cert_file, key_file = load_cert(cuit_emisor)

    # 1) Token AFIP (puede lanzar excepción)
    token, sign = get_token_sign(cert_file, key_file)

    # 2) Cliente WSFE
    client = get_wsfe_client(token, sign, cuit_emisor)

    # 3) Último comprobante
    try:
        ultimo = client.service.FECompUltimoAutorizado(
            punto_venta, tipo_cbte
        )
        cbte_nro = ultimo.CbteNro + 1
    except WebFault as e:
        error_msg = e.fault.faultstring
        raise Exception(f"Error al obtener último comprobante (AFIP): {error_msg}")


    # 4) Comprobante (ESTRUCTURA CORRECTA AFIP)
    fe_cae_req = {
        "FeCabReq": {
            "CantReg": 1,
            "PtoVta": punto_venta,
            "CbteTipo": tipo_cbte,
        },
        "FeDetReq": [{
            "Concepto": 1,
            "DocTipo": 80,  # CUIT
            "DocNro": int(cuit_receptor),
            "CbteDesde": cbte_nro,
            "CbteHasta": cbte_nro,
            "CbteFch": int(datetime.datetime.now().strftime("%Y%m%d")),
            "ImpTotal": importe,
            "ImpNeto": importe,
            "ImpIVA": 0,
            "MonId": "PES",
            "MonCotiz": 1,
        }]
    }

    # 5) Solicitar CAE
    try:
        res = client.service.FECAESolicitar(
            {"FeCAEReq": fe_cae_req}
        )
        det = res.FeDetResp[0]
    except WebFault as e:
        error_msg = e.fault.faultstring
        raise Exception(f"Error al solicitar CAE (AFIP): {error_msg}")
    
    
    if not det.CAE:
        if hasattr(det, 'Observaciones') and det.Observaciones:
            raise Exception(f"AFIP rechazó la factura: {det.Observaciones[0].Msg}")
        else:
            raise Exception("AFIP rechazó la factura sin mensaje de error claro.")


    return {
        "cbte_nro": cbte_nro,
        "cae": det.CAE,
        "vencimiento": det.CAEFchVto,
    }


# ----------------------------------------------------------------------
# ENDPOINTS
# ----------------------------------------------------------------------

@app.route("/facturar", methods=["POST"])
def facturar():
    try:
        data = request.json
        factura = crear_factura(data)
        return jsonify({"status": "OK", "factura": factura})
    except Exception as e:
        return jsonify({"status": "ERROR", "detalle": str(e)})


@app.route("/", methods=["GET"])
def home():
    return "AFIP Microserver funcionando."


# ----------------------------------------------------------------------
# LOCAL DEBUG
# ----------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
