import os
import io
import re
import requests
import cloudinary
import cloudinary.uploader
from flask import Flask, request, jsonify
from openai import OpenAI
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

app = Flask(__name__)

GHL_WEBHOOK_URL = os.environ.get("GHL_WEBHOOK_URL", "https://services.leadconnectorhq.com/hooks/YOUR_INBOUND_WEBHOOK_ID")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

cloudinary.config(cloudinary_url=os.environ.get("CLOUDINARY_URL"))

def sanitize_filename_part(text):
    if not text:
        return ""
    clean_str = re.sub(r'[^\w\s-]', '', str(text)).strip()
    return re.sub(r'[-\s]+', '_', clean_str)

def generate_ai_analysis(data):
    if not OPENAI_API_KEY:
        return "Custom HVAC contractor growth audit detailing Google Local Services setup and review automation tactics."

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        prompt = (
            f"Generate a 3-paragraph local service marketing audit for an HVAC company named '{data.get('company', 'HVAC Business')}'. "
            f"Owner: {data.get('name', 'Valued Contractor')}. Include strategies for emergency service PPC, seasonal tune-up promotions, and Google review generation."
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=350
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"HVAC Contracting Audit: Local lead acquisition roadmap. (AI Note: {str(e)})"

def build_pdf_buffer(data):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=20, textColor=colors.HexColor("#b91c1c"), spaceAfter=12)
    body_style = ParagraphStyle('Body', parent=styles['Normal'], fontSize=11, spaceAfter=8)

    ai_report_text = generate_ai_analysis(data)

    story = [
        Paragraph("Smart1 HVAC — Contracting Growth & AI Audit", title_style),
        Spacer(1, 12),
        Paragraph(f"<b>Contractor Name:</b> {data.get('name', 'N/A')}", body_style),
        Paragraph(f"<b>Email:</b> {data.get('email', 'N/A')}", body_style),
        Paragraph(f"<b>Company Name:</b> {data.get('company', 'N/A')}", body_style),
        Spacer(1, 14),
        Paragraph("<b>AI Growth Recommendations:</b>", styles['Heading2']),
        Paragraph(ai_report_text.replace('\n', '<br/>'), body_style)
    ]
    doc.build(story)
    buffer.seek(0)
    return buffer

def upload_to_cloudinary(pdf_buffer, file_name):
    try:
        response = cloudinary.uploader.upload(
            pdf_buffer,
            resource_type="raw",
            public_id=f"reports/smarthvac/{file_name}.pdf",
            overwrite=True
        )
        return response.get("secure_url")
    except Exception as e:
        print(f"Cloudinary upload error: {e}")
        return None

@app.route('/api/submit-lead', methods=['POST'])
def submit_lead():
    try:
        data = request.json or request.form.to_dict()
        if not data:
            return jsonify({"status": "error", "message": "No data provided"}), 400

        client_email = data.get("email", "client").strip()
        client_name = sanitize_filename_part(data.get("name", "lead"))
        company_name = sanitize_filename_part(data.get("company", data.get("company_name", "")))

        if company_name:
            file_identifier = f"smarthvac_{company_name}_{client_name}_{client_email}"
        else:
            file_identifier = f"smarthvac_{client_name}_{client_email}"

        pdf_buffer = build_pdf_buffer(data)
        cloudinary_url = upload_to_cloudinary(pdf_buffer, file_identifier)

        base_url = request.host_url.rstrip('/')
        pdf_url = cloudinary_url or f"{base_url}/api/download-report?email={client_email}"

        ghl_payload = {
            "opportunity_name": f"{data.get('company', data.get('name', 'Client'))} - Smart1 HVAC Lead",
            "client_name": data.get("name", ""),
            "client_email": client_email,
            "client_phone": data.get("phone", ""),
            "company_name": data.get("company", ""),
            "client_pdf_url": pdf_url,
            "source": "Smart HVAC Landing Page",
            "campaign_data": data
        }

        ghl_res_code = None
        if GHL_WEBHOOK_URL and "YOUR_INBOUND_WEBHOOK_ID" not in GHL_WEBHOOK_URL:
            res = requests.post(GHL_WEBHOOK_URL, json=ghl_payload, timeout=10)
            ghl_res_code = res.status_code

        return jsonify({
            "status": "success",
            "client_pdf_url": pdf_url,
            "cloudinary_upload": bool(cloudinary_url),
            "ghl_status_code": ghl_res_code
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
