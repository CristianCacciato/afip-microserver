from flask import Flask, request, jsonify
from suds.client import Client
from suds import WebFault
import datetime
import os
import base64
import subprocess # Importación para manejo seguro de openssl


app = Flask(__name__)

# ----------------------------------------------------------------------
# CONFIGURACIÓN CUITS Y CERTIFICADOS
# (los archivos deben estar en la raíz del repo)
#
# !! ATENCIÓN: Asegúrate de que estos nombres coincidan EXACTAMENTE
# !! con los archivos subidos a tu entorno de Render.
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

def create_cms(cert_file, key_file):
    # Utilizamos subprocess para ejecutar openssl y capturar la salida y errores
    cmd_list = [
        "openssl", "cms", "-sign", "-in", "tra.xml",
        "-signer", cert_file, "-inkey", key_file,
        "-nodetach", "-outform", "der"
    ]
    
    try:
        # Ejecuta el comando y captura la salida binaria
        result = subprocess.run(cmd_list, capture_output=True, check=True)
        cms_bin = result.stdout
        
    except subprocess.CalledProcessError as e:
        # Si openssl falla (ej: no encuentra archivos), captura el error real
        error_message = e.stderr.decode('utf-8', errors='ignore')
        raise Exception(f"Error OpenSSL al firmar: {error_message}. Revise nombres/permisos de los archivos de certificado y clave.")

    # Base64 SIN saltos de línea del binario capturado
    cms_b64 = base64.b64encode(cms_bin).decode("ascii")

    return cms_b64


def get_token_sign(cert_file, key_file):
    # (El código de generación de TRA sigue igual)
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

    with open("tra.xml", "w") as f:
        f.write(tra)

    cms = create_cms(cert_file, key_file)

    client = Client(WSAA)
    
    try:
        # Llamada al servicio WSAA (Autenticación)
        ta = client.service.loginCms(cms)
        
        # Si la llamada es exitosa
        return ta.credentials.token, ta.credentials.sign

    except WebFault as e:
        # Captura errores de la AFIP (e.g., "Computador no autorizado")
        error_msg = e.fault.faultstring
        raise Exception(f"Error WSAA (AFIP): {error_msg}. Revisar configuración de relación de certificado.")
    
    except Exception as e:
        # Captura cualquier otro error, incluido el error de suds.
        # Aquí intentamos obtener un mensaje más detallado.
        return ta # <-- Si se lanza un error antes, `ta` podría ser la respuesta de texto.
        raise Exception(f"Error desconocido en WSAA. Detalle: {str(e)}. Intente volver a desplegar.")

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
        # Si el CAE es nulo, buscamos la observación/error de la AFIP
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
        # Todos los errores específicos (OpenSSL, WSAA, WSFE) son devueltos aquí
        return jsonify({"status": "ERROR", "detalle": str(e)})


@app.route("/", methods=["GET"])
def home():
    return "AFIP Microserver funcionando."


# ----------------------------------------------------------------------
# LOCAL DEBUG
# ----------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
