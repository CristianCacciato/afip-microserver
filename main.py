from flask import Flask, request, jsonify
from suds.client import Client
import datetime
import os
import base64


app = Flask(__name__)

# ----------------------------------------------------------------------
# CONFIGURACIÓN CUITS Y CERTIFICADOS
# (los archivos deben estar en la raíz del repo)
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

# HOMOLOGACIÓN
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
    cmd = (
        f"openssl cms -sign -in tra.xml "
        f"-signer {cert_file} -inkey {key_file} "
        f"-nodetach -outform der > cms.der"
    )
    os.system(cmd)

    # leer binario
    with open("cms.der", "rb") as f:
        cms_bin = f.read()

    # base64 SIN saltos de línea
    cms_b64 = base64.b64encode(cms_bin).decode("ascii")

    return cms_b64


import datetime # Asegúrate que esta importación esté al inicio
# ...
def get_token_sign(cert_file, key_file):
    # Usar UTC/utcnow() para evitar problemas de zona horaria y añadir 'Z'
    now = datetime.datetime.utcnow()
    
    # Generation Time: Ahora
    generation_time = now.strftime('%Y-%m-%dT%H:%M:%S.000Z')

    # Expiration Time: 10 minutos en el futuro
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
    ta = client.service.loginCms(cms)

    return ta.credentials.token, ta.credentials.sign


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

    # 1) Token AFIP
    token, sign = get_token_sign(cert_file, key_file)

    # 2) Cliente WSFE
    client = get_wsfe_client(token, sign, cuit_emisor)

    # 3) Último comprobante
    ultimo = client.service.FECompUltimoAutorizado(
        punto_venta, tipo_cbte
    )
    cbte_nro = ultimo.CbteNro + 1

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
    res = client.service.FECAESolicitar(
        {"FeCAEReq": fe_cae_req}
    )

    det = res.FeDetResp[0]

    if not det.CAE:
        raise Exception(det.Observaciones[0].Msg)

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
