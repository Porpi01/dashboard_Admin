from fastapi import FastAPI, HTTPException
from pymongo import MongoClient
from pymongo.server_api import ServerApi
from dotenv import load_dotenv
import os
from typing import List, Dict, Any, Optional
from datetime import datetime
from bson.objectid import ObjectId
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pandas as pd
from io import BytesIO
from fastapi.responses import StreamingResponse, FileResponse # Importar FileResponse
from fastapi.staticfiles import StaticFiles

# Cargar variables de entorno desde .env
load_dotenv()

app = FastAPI(
    title="API de Dashboard de Startups y Sesiones",
    description="API para servir datos de MongoDB a un dashboard de gestión de startups y mentorías.",
    version="1.0.0"
)



app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # CONSIDERAR CAMBIAR EN PRODUCCIÓN a 'origins' variable
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Si planeas servir archivos estáticos desde FastAPI (como tu index.html, script.js, style.css)
# asegúrate de que la carpeta 'static' exista en la raíz de tu proyecto.
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- Conexión a MongoDB Atlas ---
client: Optional[MongoClient] = None
db: Any = None # Usamos Any para flexibilidad con las colecciones

@app.on_event("startup")
async def startup_db_client():
    global client, db
    try:
        mongo_url = os.getenv("DATABASE_URL")
        if not mongo_url:
            print("WARNING: DATABASE_URL no encontrada en .env. Usando una URL de fallback o generando un error.")
            raise ValueError("DATABASE_URL no está configurada en el archivo .env")

        client = MongoClient(mongo_url, server_api=ServerApi('1'))
        client.admin.command('ping')
        db = client["Cluster0"] # Confirma que "Cluster0" sea el nombre correcto de tu base de datos
        print("Conexión a MongoDB Atlas establecida con éxito.")
    except Exception as e:
        print(f"ERROR: No se pudo conectar a MongoDB Atlas: {e}")
        client = None
        db = None # Asegurar que db sea None si la conexión falla

@app.on_event("shutdown")
async def shutdown_db_client():
    if client:
        client.close()
        print("Conexión a MongoDB Atlas cerrada.")

# --- Funciones Auxiliares para Datos ---
def serialize_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convierte ObjectId y datetime a string para JSON-like display,
    recursivamente para diccionarios anidados y listas.
    """
    if '_id' in doc:
        doc['_id'] = str(doc['_id'])
    # Iterar sobre una copia de las claves para poder modificar el diccionario durante la iteración
    for key, value in list(doc.items()):
        if isinstance(value, ObjectId):
            doc[key] = str(value)
        elif isinstance(value, datetime):
            # Formatear la fecha para que sea más legible en el frontend.
            # Puedes usar .isoformat() si prefieres el estándar ISO 8601.
            doc[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(value, dict):
            # Esto maneja los campos 'signed' si se devuelven como dicts por error en algún otro endpoint
            if key in ["mentorSigned", "startupSigned"] and "signed" in value:
                doc[key] = bool(value.get("signed", False)) # Asegura que sea booleano
            else:
                doc[key] = serialize_doc(value)
        elif isinstance(value, list):
            doc[key] = [serialize_doc(item) if isinstance(item, dict) else item for item in value]
    return doc

def get_collection_data(collection_name: str) -> List[Dict[str, Any]]:
    if db is None:
        raise HTTPException(status_code=503, detail="Base de datos no disponible.")
    
    collection = db[collection_name]
    data = list(collection.find({}))
    return [serialize_doc(doc) for doc in data]

# Modelo Pydantic para la respuesta de sesiones
class SessionDetails(BaseModel):
    _id: str
    mentor_id: Optional[str] = None
    CompanyName: Optional[str] = None
    startup_id: Optional[str] = None
    startup_company: Optional[str] = None
    date: Optional[str] = None # Cambiado a str para manejar el formato ISO
    topic: Optional[str] = None
    duration: Optional[int] = None
    summary: Optional[str] = None
    status: Optional[str] = None
    comments: Optional[List[str]] = None
    pdfUrl: Optional[str] = None
    mentorSigned: Optional[bool] = None # Pydantic espera un booleano aquí
    startupSigned: Optional[bool] = None # Pydantic espera un booleano aquí

# --- Endpoints de la API ---

# CORRECCIÓN CLAVE: Servir index.html en la ruta raíz
@app.get("/", tags=["Root"])
async def serve_frontend():
    """Serves the main frontend application (index.html)."""
    try:
        # Asegúrate de que 'index.html' esté en la carpeta 'static'
        # o ajusta la ruta si está en otro lugar
        return FileResponse("static/index.html")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="index.html not found in static directory.")



@app.get("/api/startups", response_model=List[Dict[str, Any]], summary="Obtiene todas las startups")
async def get_all_startups():
    try:
        # Asegúrate de que el nombre de la colección sea 'startup' (en minúsculas)
        return get_collection_data("startup") 
    except HTTPException as e:
        raise e # Re-lanza HTTPException para que FastAPI la maneje
    except Exception as e:
        print(f"Error interno al obtener startups: {e}") # Log del error real
        raise HTTPException(status_code=500, detail=f"Error interno al obtener startups: {e}")

@app.get("/api/mentors", response_model=List[Dict[str, Any]], summary="Obtiene todos los mentores")
async def get_all_mentors():
    try:
        # Asegúrate de que el nombre de la colección sea 'mentorship' (en minúsculas)
        return get_collection_data("mentorship") 
    except HTTPException as e:
        raise e
    except Exception as e:
        print(f"Error interno al obtener mentores: {e}")
        raise HTTPException(status_code=500, detail=f"Error interno al obtener mentores: {e}")

@app.get("/api/sessions/detailed", response_model=List[SessionDetails], summary="Obtiene todas las sesiones con detalles de startup y mentor")
async def get_all_sessions_detailed():
    if db is None:
        raise HTTPException(status_code=503, detail="Base de datos no disponible.")
    
    pipeline = [
        {"$addFields": {
            # Intenta convertir 'mentor' a ObjectId. Si no es un ObjectId válido o es null, será null.
            "mentorObjectId": {"$toObjectId": "$mentor"}
        }},
        {"$lookup": {
            "from": "mentorship", # Colección de mentores
            "localField": "mentorObjectId",
            "foreignField": "_id",
            "as": "mentor_info"
        }},
        {"$unwind": {"path": "$mentor_info", "preserveNullAndEmptyArrays": True}},
        
        {"$addFields": {
            # Intenta convertir 'startup' a ObjectId. Si no es un ObjectId válido o es null, será null.
            "startupObjectId": {"$toObjectId": "$startup"}
        }},
        {"$lookup": {
            "from": "startup", # Colección de startups
            "localField": "startupObjectId",
            "foreignField": "_id",
            "as": "startup_info"
        }},
        {"$unwind": {"path": "$startup_info", "preserveNullAndEmptyArrays": True}},
        
        {"$project": {
            "_id": {"$toString": "$_id"},
            "mentor_id": {"$toString": {"$ifNull": ["$mentor_info._id", None]}},
            # Preferir 'company' del mentor, si no 'name' o un valor por defecto
            "CompanyName": {"$ifNull": ["$mentor_info.company", "$mentor_info.name", "Compañía Mentor Desconocida"]},
            "startup_id": {"$toString": {"$ifNull": ["$startup_info._id", None]}},
            # Preferir 'name' de la startup, si no 'company' o un valor por defecto
            "startup_company": {"$ifNull": ["$startup_info.name", "$startup_info.company", "Startup Desconocida"]},
            "date": {"$dateToString": {"format": "%Y-%m-%d %H:%M:%S", "date": "$date"}},
            "topic": "$topic",
            "duration": "$duration",
            "summary": "$summary",
            "status": "$status",
            "comments": {"$ifNull": ["$comments", []]},
            "pdfUrl": {"$ifNull": ["$pdfUrl", None]},
            # --- CORRECCIÓN CLAVE AQUÍ ---
            # Extraer el valor 'signed' de los subdocumentos 'mentorSigned' y 'startupSigned'.
            # Utiliza '$ifNull' para proporcionar un valor predeterminado (False) si el subdocumento
            # o el campo 'signed' no existen, asegurando que siempre sea un booleano.
            "mentorSigned": {"$ifNull": ["$mentorSigned.signed", False]},
            "startupSigned": {"$ifNull": ["$startupSigned.signed", False]},
            # --- FIN CORRECCIÓN CLAVE ---
        }},
        {"$sort": {"date": -1}}
    ]
    
    try:
        data = list(db["sessions"].aggregate(pipeline))
        # Pydantic validará la estructura de cada elemento en la lista.
        # Si el pipeline es correcto, los datos ya deberían ajustarse a SessionDetails.
        return [SessionDetails(**doc) for doc in data] 
    except Exception as e:
        print(f"Error en la agregación de sesiones: {e}") # Log del error real en el servidor
        raise HTTPException(status_code=500, detail=f"Error al ejecutar la agregación de sesiones: {e}. Revisa el formato de los IDs y los campos 'signed' en tu DB.")


@app.get("/api/sessions/download_excel", summary="Descarga todas las sesiones con detalles en formato Excel")
async def download_sessions_excel():
    if db is None:
        raise HTTPException(status_code=503, detail="Base de datos no disponible.")
    
    # Reutilizamos la lógica del endpoint detailed para obtener los datos
    sessions_data = await get_all_sessions_detailed()
    
    if not sessions_data:
        raise HTTPException(status_code=404, detail="No hay datos de sesiones para descargar.")

    # Convertir a formato que pandas pueda usar fácilmente
    df = pd.DataFrame([s.dict() for s in sessions_data]) # Usar .dict() en los modelos Pydantic
    
    # Excluir los IDs internos de MongoDB si no son relevantes para el informe
    df = df.drop(columns=['_id', 'mentor_id', 'startup_id'], errors='ignore')

    # Renombrar columnas para el Excel si es necesario
    df = df.rename(columns={
        "CompanyName": "Compañía Mentor",
        "startup_company": "Compañía Startup",
        "date": "Fecha",
        "topic": "Tema",
        "duration": "Duración (min)",
        "summary": "Resumen",
        "status": "Estado",
        "comments": "Comentarios",
        "pdfUrl": "URL Documento",
        "mentorSigned": "Mentor Firmó",
        "startupSigned": "Startup Firmó"
    })

    # Convertir la columna de comentarios de lista a string
    df['Comentarios'] = df['Comentarios'].apply(lambda x: ', '.join(x) if isinstance(x, list) else x)

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Sesiones')
    output.seek(0)

    # Configurar la respuesta para la descarga del archivo
    filename = f"sesiones_mentorias_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Access-Control-Expose-Headers": "Content-Disposition" # Esto es crucial para CORS en descargas
        }
    )