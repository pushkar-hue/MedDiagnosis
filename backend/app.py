import os
import torch
import torchvision
from torchvision import transforms
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from PIL import Image
import json
from datetime import datetime
import google.generativeai as genai
from dotenv import load_dotenv
import base64
from report import generate_professional_report
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, get_jwt_identity, jwt_required
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta
import uuid
from flask_socketio import SocketIO, emit, join_room, leave_room
import time
import secrets



app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Load environment variables
load_dotenv()

# Configure Gemini API
genai.configure(api_key="")
gemini_model = genai.GenerativeModel('gemini-1.5-pro')

video_rooms = {}

app.config['JWT_SECRET_KEY'] = ''  # Change this to a secure random key
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=24)
jwt = JWTManager(app)




app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg'}


socketio = SocketIO(app, cors_allowed_origins="*")

# Simple in-memory chat storage (use a database in production)
chat_messages = {}
chat_rooms = {}


# Create upload folder if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs("reports", exist_ok=True)  # Ensure the reports folder exists

# Define device
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Define model information
MODELS = {
    'breast_cancer': {
        'file': 'models/breast_cancer_model.pth',
        'classes': ['Healthy', 'Sick'],
        'display_name': 'Breast Cancer Detection',
        'description': 'Detects potential breast cancer from histopathology images'
    },
    'covid19': {
        'file': 'models/covid19_model.pth',
        'classes': ['COVID', 'Lung_Opacity', 'Normal', 'Viral Pneumonia'],
        'display_name': 'COVID-19 Analysis',
        'description': 'Analyzes chest X-rays for COVID-19 and other lung conditions'
    },
    'malaria': {
        'file': 'models/malaria_model.pth',
        'classes': ['Parasitized', 'Uninfected'],
        'display_name': 'Malaria Detection',
        'description': 'Identifies malaria parasites in blood smear images'
    },
    'pneumonia': {
        'file': 'models/pneumonia_model.pth',
        'classes': ['NORMAL', 'PNEUMONIA'],
        'display_name': 'Pneumonia Detection',
        'description': 'Detects pneumonia in chest X-ray images'
    },
    'skin_cancer': {
        'file': 'models/skin_cancer_model.pth',
        'classes': ['benign', 'malignant'],
        'display_name': 'Skin Cancer Classification',
        'description': 'Classifies skin lesions as benign or malignant'
    },
    'tuberculosis': {
        'file': 'models/tuberculosis_model.pth',
        'classes': ['Normal', 'Tuberculosis'],
        'display_name': 'Tuberculosis Screening',
        'description': 'Screens chest X-rays for signs of tuberculosis'
    }
}

# Initialize model cache
model_cache = {}

# Create a simple user database (in a real app, use a proper database)
users = {
    # Example users
    "user1@example.com": {
        "id": "user-1",
        "name": "John Doe",
        "email": "user1@example.com",
        "password": generate_password_hash("password123"),
        "role": "patient"
    },
    "doctor1@example.com": {
        "id": "doctor-1",
        "name": "Dr. Sarah Johnson",
        "email": "doctor1@example.com",
        "password": generate_password_hash("password123"),
        "role": "doctor"
    }
}

# Authentication routes
@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    email = data.get('email')
    password = data.get('password')
    name = data.get('name')
    role = data.get('role', 'patient')
    
    if not email or not password or not name:
        return jsonify({"error": "Missing required fields"}), 400
    
    if email in users:
        return jsonify({"error": "Email already registered"}), 400
    
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    users[email] = {
        "id": user_id,
        "name": name,
        "email": email,
        "password": generate_password_hash(password),
        "role": role
    }
    
    access_token = create_access_token(identity=email)
    
    return jsonify({
        "id": user_id,
        "name": name,
        "email": email,
        "role": role,
        "token": access_token
    }), 201

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email')
    password = data.get('password')
    
    if not email or not password:
        return jsonify({"error": "Missing email or password"}), 400
    
    user = users.get(email)
    if not user or not check_password_hash(user['password'], password):
        return jsonify({"error": "Invalid email or password"}), 401
    
    access_token = create_access_token(identity=email)
    
    return jsonify({
        "id": user['id'],
        "name": user['name'],
        "email": user['email'],
        "role": user['role'],
        "token": access_token
    }), 200

@app.route('/api/user', methods=['GET'])
@jwt_required()
def get_user():
    current_user_email = get_jwt_identity()
    user = users.get(current_user_email)
    
    if not user:
        return jsonify({"error": "User not found"}), 404
    
    return jsonify({
        "id": user['id'],
        "name": user['name'],
        "email": user['email'],
        "role": user['role']
    }), 200


@app.route('/api/chat/history/<room_id>', methods=['GET'])
@jwt_required()
def get_chat_history(room_id):
    current_user_email = get_jwt_identity()
    user = users.get(current_user_email)
    
    if not user:
        return jsonify({"error": "User not found"}), 404
    
    # Check if user has access to this chat room
    if room_id not in chat_rooms or user['id'] not in chat_rooms[room_id]['participants']:
        return jsonify({"error": "Access denied"}), 403
    
    messages = chat_messages.get(room_id, [])
    
    return jsonify(messages), 200

@app.route('/api/chat/rooms', methods=['GET'])
@jwt_required()
def get_chat_rooms():
    current_user_email = get_jwt_identity()
    user = users.get(current_user_email)
    
    if not user:
        return jsonify({"error": "User not found"}), 404
    
    user_rooms = []
    for room_id, room in chat_rooms.items():
        if user['id'] in room['participants']:
            # Get the other participant
            other_participant_id = next((p for p in room['participants'] if p != user['id']), None)
            other_participant = next((u for u in users.values() if u['id'] == other_participant_id), None)
            
            # Get the last message
            messages = chat_messages.get(room_id, [])
            last_message = messages[-1] if messages else None
            
            user_rooms.append({
                "id": room_id,
                "name": room['name'],
                "other_participant": {
                    "id": other_participant['id'],
                    "name": other_participant['name'],
                    "role": other_participant['role']
                } if other_participant else None,
                "last_message": last_message,
                "unread_count": sum(1 for m in messages if not m['read'] and m['sender_id'] != user['id'])
            })
    
    return jsonify(user_rooms), 200

# Socket.IO events
@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

@socketio.on('join')
def handle_join(data):
    room = data['room']
    join_room(room)
    print(f'Client joined room: {room}')

@socketio.on('leave')
def handle_leave(data):
    room = data['room']
    leave_room(room)
    print(f'Client left room: {room}')

@socketio.on('message')
def handle_message(data):
    room_id = data['room']
    message = data['message']
    sender_id = data['sender_id']
    sender_name = data['sender_name']
    
    # Create message object
    msg = {
        'id': str(uuid.uuid4()),
        'room_id': room_id,
        'sender_id': sender_id,
        'sender_name': sender_name,
        'content': message,
        'timestamp': time.time(),
        'read': False
    }
    
    # Store message
    if room_id not in chat_messages:
        chat_messages[room_id] = []
    chat_messages[room_id].append(msg)
    
    # Broadcast to room
    emit('message', msg, room=room_id)



@app.route('/api/video/create-room', methods=['POST'])
@jwt_required()
def create_video_room():
    current_user_email = get_jwt_identity()
    user = users.get(current_user_email)
    
    if not user:
        return jsonify({"error": "User not found"}), 404
    
    # Generate a unique room ID
    room_id = f"room-{secrets.token_hex(6)}"
    
    # Store room information
    video_rooms[room_id] = {
        'id': room_id,
        'creator': user['id'],
        'created_at': time.time(),
        'participants': [user['id']],
        'active': True
    }
    
    return jsonify({
        'room_id': room_id,
        'creator': user['name'],
        'created_at': video_rooms[room_id]['created_at']
    }), 201

@app.route('/api/video/join-room/<room_id>', methods=['POST'])
@jwt_required()
def join_video_room(room_id):
    current_user_email = get_jwt_identity()
    user = users.get(current_user_email)
    
    if not user:
        return jsonify({"error": "User not found"}), 404
    
    if room_id not in video_rooms:
        return jsonify({"error": "Room not found"}), 404
    
    if not video_rooms[room_id]['active']:
        return jsonify({"error": "Room is no longer active"}), 400
    
    # Add user to participants if not already there
    if user['id'] not in video_rooms[room_id]['participants']:
        video_rooms[room_id]['participants'].append(user['id'])
    
    return jsonify({
        'room_id': room_id,
        'creator': next((u['name'] for u in users.values() if u['id'] == video_rooms[room_id]['creator']), None),
        'participants': [
            {
                'id': p,
                'name': next((u['name'] for u in users.values() if u['id'] == p), None)
            }
            for p in video_rooms[room_id]['participants']
        ]
    }), 200

@app.route('/api/video/end-room/<room_id>', methods=['POST'])
@jwt_required()
def end_video_room(room_id):
    current_user_email = get_jwt_identity()
    user = users.get(current_user_email)
    
    if not user:
        return jsonify({"error": "User not found"}), 404
    
    if room_id not in video_rooms:
        return jsonify({"error": "Room not found"}), 404
    
    # Only the creator can end the room
    if user['id'] != video_rooms[room_id]['creator']:
        return jsonify({"error": "Only the creator can end the room"}), 403
    
    video_rooms[room_id]['active'] = False
    
    return jsonify({'success': True}), 200

# Socket.IO events for video call signaling
@socketio.on('video-offer')
def handle_video_offer(data):
    room = data['room']
    emit('video-offer', data, room=room, include_self=False)

@socketio.on('video-answer')
def handle_video_answer(data):
    room = data['room']
    emit('video-answer', data, room=room, include_self=False)

@socketio.on('ice-candidate')
def handle_ice_candidate(data):
    room = data['room']
    emit('ice-candidate', data, room=room, include_self=False)

@socketio.on('leave-room')
def handle_leave_room(data):
    room = data['room']
    leave_room(room)
    emit('user-left', {'user_id': data['user_id']}, room=room)


def load_model(model_key):
    """Load model if not already in cache"""
    if model_key not in model_cache:
        print(f"Loading model: {model_key}")
        
        # Initialize model architecture (using EfficientNet B0 for all models)
        model = torchvision.models.efficientnet_b0(weights=None)
        num_classes = len(MODELS[model_key]['classes'])
        
        model.classifier = torch.nn.Sequential(
                                    torch.nn.Dropout(p=0.5),
                                    torch.nn.Linear(model.classifier[1].in_features, num_classes)
                                )
        
        # Load the saved weights
        model_path = MODELS[model_key]['file']
        try:
            model.load_state_dict(torch.load(model_path, map_location=device))
            model.to(device)
            model.eval()
            model_cache[model_key] = model
            print(f"Model {model_key} loaded successfully")
        except Exception as e:
            print(f"Error loading model {model_key}: {str(e)}")
            return None
    
    return model_cache[model_key]

# Define image transformation for inference
test_transform = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
])

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def generate_enhanced_report(report_data, image_path, save_path):
    """
    Generate a comprehensive medical report using Gemini API.
    
    :param report_data: Dictionary containing prediction, confidence, and analysis.
    :param image_path: Path to the uploaded patient image.
    :param save_path: Path to save the generated PDF.
    """
    # Get model and prediction information
    model_name = report_data['display_name']
    prediction = report_data['prediction']
    confidence = report_data['confidence']
    
    # Create prompt for Gemini API
    prompt = f"""
    You are a medical AI assistant. Based on the image analysis performed by our {model_name} model,
    generate a comprehensive medical report. The image was classified as '{prediction}' with 
    {confidence:.2f}% confidence.
    
    The report should include:
    1. A detailed explanation of the detected condition
    2. Possible symptoms associated with this condition
    3. Common treatments and next steps
    4. Risk factors and preventive measures
    5. When the patient should seek immediate medical attention
    
    Format the report professionally as it will be included in a medical PDF.
    """
    
    # Encode the image
    with open(image_path, "rb") as img_file:
        encoded_image = base64.b64encode(img_file.read()).decode('utf-8')
    
    # Generate content with Gemini
    response = gemini_model.generate_content([
        prompt,
        {"mime_type": "image/jpeg", "data": encoded_image}
    ])
    
    # Extract the generated report content
    generated_report = response.text
    
    # Now generate the PDF with this enhanced content
    generate_pdf_with_gemini(report_data, image_path, save_path, generated_report)
    
    return generated_report

def generate_pdf_with_gemini(report_data, image_path, save_path, gemini_report):
    """
    Generate a medical PDF report with Gemini-enhanced content.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Image as RLImage
    from reportlab.lib.units import inch
    
    # Create a PDF document
    doc = SimpleDocTemplate(save_path, pagesize=letter)
    story = []
    
    # Add styles
    styles = getSampleStyleSheet()
    title_style = styles['Heading1']
    heading_style = styles['Heading2']
    normal_style = styles['Normal']
    
    # Add title
    story.append(Paragraph("AI-Powered Medical Diagnosis Report", title_style))
    story.append(Spacer(1, 0.2*inch))
    
    # Add date
    story.append(Paragraph(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", normal_style))
    story.append(Spacer(1, 0.1*inch))
    
    # Add image
    img = RLImage(image_path, width=3*inch, height=2*inch)
    story.append(img)
    story.append(Spacer(1, 0.2*inch))
    
    # Add model information
    story.append(Paragraph("Analysis Information", heading_style))
    story.append(Paragraph(f"Model Used: {report_data['display_name']}", normal_style))
    story.append(Paragraph(f"Detected Condition: {report_data['prediction']}", normal_style))
    story.append(Paragraph(f"Confidence: {report_data['confidence']:.2f}%", normal_style))
    
    # Add risk level
    risk_level = "High Risk" if report_data['confidence'] > 85 else "Moderate Risk" if report_data['confidence'] > 50 else "Low Risk"
    story.append(Paragraph(f"Risk Assessment: {risk_level}", normal_style))
    story.append(Spacer(1, 0.2*inch))
    
    # Add Gemini-generated report
    story.append(Paragraph("Detailed Medical Analysis", heading_style))
    # Split the report into paragraphs and add them
    for paragraph in gemini_report.split('\n\n'):
        if paragraph.strip():
            story.append(Paragraph(paragraph, normal_style))
            story.append(Spacer(1, 0.1*inch))
    
    # Disclaimer
    story.append(Spacer(1, 0.2*inch))
    disclaimer_style = ParagraphStyle(
        'Disclaimer', 
        parent=normal_style, 
        fontName='Helvetica-Oblique',
        fontSize=8
    )
    story.append(Paragraph("DISCLAIMER: This report is AI-generated and should not replace professional medical advice. Please consult with a healthcare provider for proper diagnosis and treatment.", disclaimer_style))
    
    # Build the PDF
    doc.build(story)

def get_gemini_chat_response(user_message):
    """Get response from Gemini API for chat"""
    # Create a medical context prompt
    context = """You are a helpful medical assistant chatbot for a medical diagnosis application. 
    Your purpose is to answer medical questions, explain conditions, and provide general health information.
    You should NOT attempt to diagnose specific conditions or prescribe treatments.
    Always clarify that users should consult healthcare professionals for proper diagnosis and treatment.
    Keep responses concise, accurate, and empathetic."""
    
    # Combine context and user message
    prompt = f"{context}\n\nUser: {user_message}\n\nAssistant:"
    
    # Call Gemini API
    response = gemini_model.generate_content(prompt)
    
    return response.text

@app.route('/')
def index():
    # Pass model information to the template
    return render_template('index.html', models=MODELS)

@app.route('/chat', methods=['GET'])
def chat_interface():
    """Render the chat interface page"""
    return render_template('chat.html')

@app.route('/api/chat', methods=['POST'])
def chat_with_gemini():
    """API endpoint for the chatbot"""
    data = request.json
    user_message = data.get('message', '')
    
    if not user_message:
        return jsonify({'error': 'No message provided'}), 400
    
    try:
        # Call Gemini API for chat response
        chat_response = get_gemini_chat_response(user_message)
        return jsonify({'response': chat_response})
    except Exception as e:
        print(f"Error in chat processing: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/models')
def get_models():
    return jsonify(MODELS)

@app.route('/predict', methods=['POST'])
def predict():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    
    file = request.files['file']
    model_key = request.form.get('model', 'skin_cancer')
    
    if model_key not in MODELS:
        return jsonify({'error': 'Invalid model selection'}), 400
    
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        try:
            # Load the selected model
            model = load_model(model_key)
            if model is None:
                return jsonify({'error': f'Failed to load model: {model_key}'}), 500
            
            # Open and transform the image
            img = Image.open(filepath).convert('RGB')
            img_tensor = test_transform(img).unsqueeze(0).to(device)
            
            # Get prediction
            with torch.no_grad():
                output = model(img_tensor)
                probabilities = torch.nn.functional.softmax(output, dim=1)[0]
                prediction = torch.argmax(probabilities).item()
            
            # Get class names for this model    
            class_names = MODELS[model_key]['classes']
            
            # Generate a PDF Report
            pdf_filename = f"report_{filename.rsplit('.', 1)[0]}.pdf"
            pdf_path = os.path.join("reports", pdf_filename)
            
            # Format the result
            result = {
                'prediction': class_names[prediction],
                'confidence': float(probabilities[prediction]) * 100,
                'model_used': model_key,
                'display_name': MODELS[model_key]['display_name'],
                'probabilities': {
                    class_names[i]: float(prob) * 100 
                    for i, prob in enumerate(probabilities)
                },
                'report_url': f"/reports/{pdf_filename}"
            }

            # Get enhanced report content from Gemini
            gemini_report = get_gemini_report_content(result, filepath)
            
            # Generate the professional PDF report
            generate_professional_report(result, filepath, pdf_path, gemini_report)
            
            # Add the report content to the result
            result['report_content'] = gemini_report

            return jsonify(result)
        
        except Exception as e:
            print(f"Error in prediction: {str(e)}")
            return jsonify({'error': str(e)}), 500
    
    return jsonify({'error': 'Invalid file type'}), 400

def get_gemini_report_content(report_data, image_path):
    """
    Get enhanced report content from Gemini API.
    
    :param report_data: Dictionary containing prediction, confidence, and analysis.
    :param image_path: Path to the uploaded patient image.
    :return: Generated report text
    """
    # Get model and prediction information
    model_name = report_data['display_name']
    prediction = report_data['prediction']
    confidence = report_data['confidence']
    
    # Create prompt for Gemini API
    prompt = f"""
    You are a medical reporting AI assistant. Based on the image analysis performed by our {model_name} model,
    generate a professional and structured medical report. The image was classified as '{prediction}' with 
    {confidence:.2f}% confidence.
    
    Format the report with the following sections, each with the title in bold followed by a colon:
    
    **Preliminary {model_name} Report:**
    [Brief introduction about the report and its purpose]
    
    **Detected Condition:**
    [Detailed explanation of the detected condition, what it means, and important caveats about AI interpretation]
    
    **Possible Symptoms:**
    [Common symptoms associated with this condition]
    
    **Common Treatments and Next Steps:**
    [Potential treatments and immediate recommended actions]
    
    **Risk Factors and Preventive Measures:**
    [Common risk factors and how to prevent or manage the condition]
    
    **When to Seek Immediate Medical Attention:**
    [Clear guidance on when the patient should seek immediate medical care]
    
    Format each section title with double asterisks and a colon like this: **Section Title:** followed by the content.
    Make the report professional, accurate, and include appropriate medical terminology while still being understandable to patients.
    """
    
    # Encode the image
    with open(image_path, "rb") as img_file:
        encoded_image = base64.b64encode(img_file.read()).decode('utf-8')
    
    # Generate content with Gemini
    response = gemini_model.generate_content([
        prompt,
        {"mime_type": "image/jpeg", "data": encoded_image}
    ])
    
    # Extract the generated report content
    return response.text

# Route to serve uploaded images (for preview purposes)
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/reports/<filename>')
def download_report(filename):
    return send_from_directory("reports", filename, as_attachment=True)

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    # Create a models dictionary to pass to the template
    models = {
        'skin_cancer': {
            'display_name': 'Skin Cancer Detector',
            'description': 'Detects melanoma and other skin conditions',
            'classes': ['benign', 'malignant']
        },
        'covid': {
            'display_name': 'COVID-19 Detector',
            'description': 'Identifies COVID-19 from chest X-rays',
            'classes': ['Normal', 'COVID', 'Viral Pneumonia']
        },
        'pneumonia': {
            'display_name': 'Pneumonia Detector',
            'description': 'Detects pneumonia from chest X-rays',
            'classes': ['NORMAL', 'PNEUMONIA']
        },
        # Add other models as needed
    }
    
    return render_template('index.html', models=models)

@app.route('/symptom-checker')
def symptom_checker():
    """Render the symptom checker page"""
    return render_template('symptom_checker.html', models=MODELS)

@app.route('/api/check-symptoms', methods=['POST'])
def check_symptoms():
    """API endpoint for symptom analysis"""
    data = request.json
    symptoms = data.get('symptoms', '')
    age = data.get('age', '')
    gender = data.get('gender', '')
    medical_history = data.get('medicalHistory', '')
    
    if not symptoms:
        return jsonify({'error': 'No symptoms provided'}), 400
    
    try:
        # Format user information
        user_info = f"Patient Information:\nAge: {age}\nGender: {gender}\nMedical History: {medical_history}\n\nSymptoms: {symptoms}"
        
        # Create prompt for Gemini API
        prompt = f"""
        You are a medical triage assistant. Based on the following patient information and symptoms, provide:
        
        1. A list of possible conditions that match these symptoms (3-5 most likely)
        2. For each condition, indicate which of our diagnostic models would be most appropriate:
           - Breast Cancer Detection (for histopathology images)
           - COVID-19 Analysis (for chest X-rays)
           - Malaria Detection (for blood smear images)
           - Pneumonia Detection (for chest X-rays)
           - Skin Cancer Classification (for skin lesion images)
           - Tuberculosis Screening (for chest X-rays)
        3. Recommend what type of medical images would be most helpful for diagnosis
        4. Provide general precautionary advice
        
        Format your response with clearly labeled sections.
        
        IMPORTANT: Include a clear disclaimer that this is preliminary information only and not a medical diagnosis.
        
        Patient Information:
        {user_info}
        """
        
        # Call Gemini API
        response = gemini_model.generate_content(prompt)
        
        # Structure the response
        analysis = {
            'analysis': response.text,
            'recommended_models': determine_recommended_models(response.text)
        }
        
        return jsonify(analysis)
    
    except Exception as e:
        print(f"Error in symptom checking: {str(e)}")
        return jsonify({'error': str(e)}), 500

def determine_recommended_models(analysis_text):
    """Extract recommended models from the analysis text"""
    recommended = []
    
    for model_key, model_info in MODELS.items():
        if model_info['display_name'] in analysis_text:
            recommended.append({
                'key': model_key,
                'name': model_info['display_name'],
                'description': model_info['description']
            })
    
    # If no specific models were mentioned, return the first three as default options
    if not recommended:
        count = 0
        for model_key, model_info in MODELS.items():
            if count < 3:
                recommended.append({
                    'key': model_key,
                    'name': model_info['display_name'],
                    'description': model_info['description']
                })
                count += 1
    
    return recommended

if __name__ == '__main__':
    # Create a JSON file with model info for the frontend
    with open('static/model_info.json', 'w') as f:
        json.dump(MODELS, f)
    
    app.run(debug=True)
