from flask import Flask, request, jsonify
from suds.client import Client
from suds.transport.https import HttpAuthenticated
import datetime
import base64
import os

app = Flask(__name__)

# ----------------------------------------------------------------------
# CONFIGURABLE → NOMBRES DE ARCHIVOS SUBIDOS A GITHUB
# ----------------------------------------------------------------------
CUIT_1 = "27239676931"
CUIT_2 = "27461124149"

CERT_1 = "facturacion27239676931.crt"
KEY_1  = "cuit_27239676931.key"

CERT_2 = "facturacion27461124149.crt"
KEY_2  = "cuit_27461124149.key"

# AFIP endpoints
WSAA = "https://wsaa.afip.gov.ar/ws/services/LoginCms"
WSFE = "https://wswhomo.afip.gov.ar/wsfev1/service.asmx?WSDL"    # Cambiar a PRODUCCIÓN al finalizar pruebas

# ----------------------------------------------------------------------
# FUNCIONES AUXILIARES
# ----------------------------------------------------------------------

def load_cert(cuit):
    if cuit == CUIT_1:
        return CERT_1, KEY_1
    else:
        return CERT_2, KEY_2


def create_token_sign(cert_file, key_file):
    """
    Firma digital para obtener TA (Ticket de Acceso).
    """
    cms = os.popen(f'openssl cms -sign -in tra.xml -signer {cert_file} -inkey {key_file} -nodetach -outform der').read()
    return cms


def get_token_and_sign(cert_file, key_file):
    """
    Solicita TA al WSAA.
    """
    tra = f"""<loginTicketRequest version="1.0">
    <header>
        <uniqueId>{int(datetime.datetime.now().timestamp())}</uniqueId>
        <generationTime>{(datetime.datetime.now() - datetime.timedelta(minutes=10)).isoformat()}</generationTime>
        <expirationTime>{(datetime.datetime.now() + datetime.timedelta(minutes=10)).isoformat()}</expirationTime>
    </header>
    <service>wsfe</service>
</loginTicketRequest>"""

    with open("tra.xml", "w") as f:
        f.write(tra)

    cms = create_token_sign(cert_file, key_file)

    client = Client(WSAA)
    resp = client.service.loginCms(cms)
    return resp


def get_wsfe_client(token, sign, cuit):
    client = Client(WSFE)
    client.set_options(soapheaders={
        "Token": token,
        "Sign": sign,
        "Cuit": cuit
    })
    return client


def create_invoice(data):
    """
    Crea factura electrónica según datos recibidos del Google Sheets.
    """
    cuit = data["cuit"]

    cert_file, key_file = load_cert(cuit)

    # 1) Obtener token & sign
    ta = get_token_and_sign(cert_file, key_file)

    token = ta.credentials.token
    sign  = ta.credentials.sign

    # 2) Conectar con WSFE
    client = get_wsfe_client(token, sign, cuit)

    # 3) Obtener último número de factura
    last = client.service.FECompUltimoAutorizado(data["punto_venta"], data["tipo_cbte"])
    next_number = last.CbteNro + 1

    # 4) Armar comprobante
    invoice = {
        "FeCabece ra": {
            "CantReg": 1,
            "PtoVta": data["punto_venta"],
            "CbteTipo": data["tipo_cbte"],
        },
        "FeDetReq": [{
            "Concepto": 1,
            "DocTipo": 99,
            "DocNro": 0,
            "CbteDesde": next_number,
            "CbteHasta": next_number,
            "CbteFch": int(datetime.datetime.now().strftime("%Y%m%d")),
            "ImpTotal": float(data["importe"]),
            "ImpNeto": float(data["importe"]),
            "ImpIVA": 0,
            "MonId": "PES",
            "MonCotiz": 1,
            "Iva": []
        }]
    }

    # 5) Llamar a AFIP
    result = client.service.FECAESolicitar(invoice)

    cae = result.FeDetResp[0].CAE
    cae_vto = result.FeDetResp[0].CAEFchVto

    return {
        "cbte_nro": next_number,
        "cae": cae,
        "vencimiento": cae_vto
    }


# ----------------------------------------------------------------------
# ENDPOINT PRINCIPAL
# ----------------------------------------------------------------------

@app.route("/facturar", methods=["POST"])
def facturar():
    data = request.json
    try:
        result = create_invoice(data)
        return jsonify({"status": "OK", "factura": result})
    except Exception as e:
        return jsonify({"status": "ERROR", "detalle": str(e)})


@app.route("/", methods=["GET"])
def home():
    return "AFIP Microserver funcionando."


# ----------------------------------------------------------------------
# RUN LOCAL (solo debug)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
