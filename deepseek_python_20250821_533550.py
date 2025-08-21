#!/usr/bin/env python3
"""
PyCine - Video Editor & Stabilizer (Perbaikan Menyeluruh)
---------------------------------------------------------------
Tool video editor dan stabilizer yang aman, efisien, dan siap produksi.

Fitur Utama:
- Stabilisasi video dengan dua metode (vidstab dan OpenCV fallback)
- Potong, gabung, ubah kecepatan, dan terapkan filter video
- Server Flask dengan autentikasi JWT dan manajemen job
- Sistem license key untuk fitur premium
- Background processing untuk operasi berat
- Manajemen resource yang aman (koneksi DB, file temporary)
- Logging komprehensif dan error handling
- Rate limiting dan validasi keamanan

Cara Penggunaan:
  # Stabilisasi video
  python3 pycine.py stabilize input.mp4 -o stable.mp4 --smoothing 30
  
  # Potong video
  python3 pycine.py trim input.mp4 10 30 -o trimmed.mp4
  
  # Jalankan server
  python3 pycine.py serve --host 0.0.0.0 --port 8080

Dependensi:
  pip install numpy moviepy vidstab flask pyjwt cryptography

License: MIT
"""

import os
import sys
import argparse
import subprocess
import tempfile
import shutil
import threading
import sqlite3
import hashlib
import json
import time
import logging
import secrets
import magic
from datetime import datetime, timedelta
from contextlib import contextmanager
from functools import wraps
from queue import Queue
from typing import List, Dict, Optional, Tuple, Any

# Konfigurasi logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(os.path.expanduser('~'), '.pycine.log')),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('pycine')

# Manajemen dependensi
dependencies = {
    'numpy': ('np', 'Numerical computing'),
    'vidstab': (None, 'Video stabilization'),
    'moviepy': (None, 'Video editing'),
    'flask': ('Flask', 'Web server'),
    'jwt': ('jwt', 'JSON Web Tokens'),
    'cryptography': (None, 'Encryption utilities')
}

missing_deps = []
for lib, (import_name, desc) in dependencies.items():
    try:
        if import_name:
            globals()[import_name] = __import__(lib)
        else:
            __import__(lib)
    except ImportError:
        missing_deps.append((lib, desc))

if missing_deps:
    logger.warning("Missing dependencies detected. Some features will be disabled.")
    for lib, desc in missing_deps:
        logger.warning("  - %s: %s (pip install %s)", lib, desc, lib)

# Set flags untuk fitur yang tersedia
_VIDSTAB_AVAILABLE = 'vidstab' not in [d[0] for d in missing_deps]
_MOVIEPY_AVAILABLE = 'moviepy' not in [d[0] for d in missing_deps]
_FLASK_AVAILABLE = 'flask' not in [d[0] for d in missing_deps]
_PYJWT_AVAILABLE = 'jwt' not in [d[0] for d in missing_deps]
_CRYPTO_AVAILABLE = 'cryptography' not in [d[0] for d in missing_deps]

# Konfigurasi aplikasi
def get_app_dirs():
    """Dapatkan direktori aplikasi mengikuti konvensi OS"""
    app_name = "pycine"
    config_dir = os.path.join(os.path.expanduser('~'), '.config', app_name)
    data_dir = os.path.join(os.path.expanduser('~'), '.local', 'share', app_name)
    cache_dir = os.path.join(os.path.expanduser('~'), '.cache', app_name)
    
    for d in [config_dir, data_dir, cache_dir]:
        os.makedirs(d, exist_ok=True)
        
    return {
        'config_dir': config_dir,
        'data_dir': data_dir,
        'cache_dir': cache_dir
    }

app_dirs = get_app_dirs()
DB_PATH = os.path.join(app_dirs['data_dir'], 'pycine_db.sqlite3')
STORAGE_DIR = os.path.join(app_dirs['data_dir'], 'uploads')
os.makedirs(STORAGE_DIR, exist_ok=True)

# Manajemen secret key
def ensure_jwt_secret():
    """Generate atau baca JWT secret key"""
    secret_path = os.path.join(app_dirs['config_dir'], 'jwt_secret')
    if os.path.exists(secret_path):
        with open(secret_path, 'r') as f:
            return f.read().strip()
    
    # Generate new secure secret
    new_secret = secrets.token_hex(32)
    with open(secret_path, 'w') as f:
        os.chmod(secret_path, 0o600)
        f.write(new_secret)
    return new_secret

JWT_SECRET = os.environ.get('PYCINE_JWT_SECRET', ensure_jwt_secret())
JWT_ALGO = 'HS256'

# ---------- Utilities ----------

def run_ffmpeg(args: List[str], check: bool = True) -> subprocess.CompletedProcess:
    """Jalankan perintah FFmpeg dengan error handling"""
    cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning'] + args
    logger.info('Running FFmpeg: %s', ' '.join(cmd))
    
    try:
        p = subprocess.run(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            timeout=3600  # Timeout 1 jam
        )
        if check and p.returncode != 0:
            logger.error('FFmpeg failed: %s', p.stderr.decode('utf-8', errors='ignore'))
            raise RuntimeError('FFmpeg failed')
        return p
    except subprocess.TimeoutExpired:
        logger.error('FFmpeg timeout after 1 hour')
        raise RuntimeError('FFmpeg timeout')
    except FileNotFoundError:
        logger.error('FFmpeg not found. Please install FFmpeg.')
        raise RuntimeError('FFmpeg not installed')

def sha256_hex(s: str) -> str:
    """Hash string dengan SHA256"""
    return hashlib.sha256(s.encode('utf-8')).hexdigest()

def allowed_file(filename: str) -> bool:
    """Periksa apakah ekstensi file diizinkan"""
    allowed_extensions = {'mp4', 'avi', 'mov', 'mkv', 'webm', 'm4v'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions

@contextmanager
def get_db_connection():
    """Context manager untuk koneksi database"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

@contextmanager
def get_db_cursor():
    """Context manager untuk database cursor dengan transaction handling"""
    with get_db_connection() as conn:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise

@contextmanager
def temporary_video_file(suffix: str = '.mp4'):
    """Context manager untuk file video temporary dengan cleanup"""
    temp_file = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    temp_path = temp_file.name
    temp_file.close()
    try:
        yield temp_path
    finally:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except Exception as e:
            logger.warning("Could not delete temp file %s: %s", temp_path, e)

# ---------- Database / License management ----------

def init_db(path: str = DB_PATH) -> None:
    """Inisialisasi database dengan tabel yang diperlukan"""
    with get_db_cursor() as cur:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, 
                username TEXT UNIQUE, 
                password_hash TEXT, 
                is_premium INTEGER, 
                created_at TEXT
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS licenses (
                id INTEGER PRIMARY KEY, 
                key TEXT UNIQUE, 
                user_id INTEGER, 
                expires_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, 
                user_id INTEGER,
                filename TEXT, 
                status TEXT, 
                result TEXT, 
                created_at TEXT,
                updated_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        logger.info("Database initialized")

def create_user(username: str, password: str, is_premium: bool = False) -> None:
    """Buat user baru"""
    ph = sha256_hex(password)
    with get_db_cursor() as cur:
        try:
            cur.execute(
                'INSERT INTO users (username, password_hash, is_premium, created_at) VALUES (?,?,?,?)', 
                (username, ph, int(is_premium), datetime.utcnow().isoformat())
            )
            logger.info("Created user: %s", username)
        except sqlite3.IntegrityError:
            raise ValueError('User already exists')

def verify_user(username: str, password: str) -> Optional[Tuple[int, bool]]:
    """Verifikasi kredensial user"""
    ph = sha256_hex(password)
    with get_db_cursor() as cur:
        cur.execute(
            'SELECT id, is_premium FROM users WHERE username=? AND password_hash=?', 
            (username, ph)
        )
        row = cur.fetchone()
        return (row['id'], bool(row['is_premium'])) if row else None

def create_license_for_user(user_id: int, days: int = 30) -> Tuple[str, str]:
    """Buat license key untuk user"""
    key = secrets.token_hex(16)
    expires = (datetime.utcnow() + timedelta(days=days)).isoformat()
    with get_db_cursor() as cur:
        cur.execute(
            'INSERT INTO licenses (key, user_id, expires_at) VALUES (?,?,?)', 
            (key, user_id, expires)
        )
    logger.info("Created license for user ID: %s", user_id)
    return key, expires

def check_license(key: str) -> bool:
    """Periksa validitas license key"""
    with get_db_cursor() as cur:
        cur.execute(
            'SELECT id, user_id, expires_at FROM licenses WHERE key=?', 
            (key,)
        )
        row = cur.fetchone()
    if not row:
        return False
    expires = datetime.fromisoformat(row['expires_at'])
    return datetime.utcnow() < expires

def get_user_from_license(key: str) -> Optional[int]:
    """Dapatkan user ID dari license key"""
    with get_db_cursor() as cur:
        cur.execute(
            'SELECT user_id FROM licenses WHERE key=?', 
            (key,)
        )
        row = cur.fetchone()
        return row['user_id'] if row else None

# ---------- Stabilizer ----------

class Stabilizer:
    """Kelas untuk stabilisasi video dengan fallback options"""
    
    def __init__(self, smoothing_window: int = 30):
        self.smoothing_window = int(smoothing_window)
        if _VIDSTAB_AVAILABLE:
            self._impl = 'vidstab'
            self.stabilizer = VidStab(
                kp_method='GFTT', 
                smoothing_window=self.smoothing_window
            )
            logger.debug("Using vidstab implementation")
        else:
            self._impl = 'opencv-simple'
            logger.debug("Using OpenCV fallback implementation")

    def stabilize(self, infile: str, outfile: str, visualize: bool = False) -> str:
        """Stabilisasi video file"""
        logger.info("Stabilizing video: %s -> %s", infile, outfile)
        
        if self._impl == 'vidstab':
            return self._stabilize_vidstab(infile, outfile, visualize)
        else:
            return self._stabilize_opencv_fallback(infile, outfile)

    def _stabilize_vidstab(self, infile: str, outfile: str, visualize: bool) -> str:
        """Stabilisasi menggunakan python-vidstab"""
        if not _VIDSTAB_AVAILABLE:
            raise RuntimeError("vidstab not available")
            
        self.stabilizer.stabilize(
            input_path=infile, 
            output_path=outfile, 
            border_type='reflect'
        )
        return outfile

    def _stabilize_opencv_fallback(self, infile: str, outfile: str) -> str:
        """Stabilisasi fallback menggunakan OpenCV murni"""
        try:
            import cv2
        except ImportError:
            logger.error("OpenCV not available for fallback stabilization")
            raise RuntimeError("OpenCV required for fallback stabilization")
            
        logger.info("Using OpenCV fallback stabilization")
        
        cap = cv2.VideoCapture(infile)
        n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

        # Baca frame pertama
        success, prev = cap.read()
        if not success:
            raise RuntimeError("Could not read video file")
            
        prev_gray = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
        transforms = np.zeros((n_frames-1, 3), np.float32)

        # Hitung transformasi antara frame
        for i in range(n_frames-1):
            success, curr = cap.read()
            if not success:
                transforms = transforms[:i]
                break
                
            curr_gray = cv2.cvtColor(curr, cv2.COLOR_BGR2GRAY)
            prev_pts = cv2.goodFeaturesToTrack(
                prev_gray, 
                maxCorners=200, 
                qualityLevel=0.01, 
                minDistance=30, 
                blockSize=3
            )
            
            if prev_pts is None:
                prev_gray = curr_gray
                continue
                
            curr_pts, status, err = cv2.calcOpticalFlowPyrLK(
                prev_gray, curr_gray, prev_pts, None
            )
            
            if curr_pts is None:
                prev_gray = curr_gray
                continue
                
            idx = np.where(status==1)[0]
            if len(idx) < 4:  # Minimal 4 points untuk estimasi transform
                prev_gray = curr_gray
                continue
                
            prev_pts_good = prev_pts[idx]
            curr_pts_good = curr_pts[idx]
            
            # Estimasi transformasi affine
            m, inliers = cv2.estimateAffinePartial2D(
                prev_pts_good, curr_pts_good
            )
            
            if m is None:
                m = np.array([[1, 0, 0], [0, 1, 0]])
                
            dx = m[0, 2]
            dy = m[1, 2]
            da = np.arctan2(m[1, 0], m[0, 0])
            transforms[i] = [dx, dy, da]
            prev_gray = curr_gray

        # Hitung dan smooth trajectory
        trajectory = np.cumsum(transforms, axis=0)
        
        def smooth(x, radius):
            window = int(radius)
            if window < 1:
                return x
            kernel = np.ones(window) / window
            return np.convolve(x, kernel, mode='same')
            
        smoothed = np.copy(trajectory)
        for i in range(3):
            smoothed[:, i] = smooth(trajectory[:, i], self.smoothing_window)
            
        diff = smoothed - trajectory
        transforms_smooth = transforms + diff

        # Terapkan transformasi yang sudah di-smooth
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        
        with temporary_video_file() as tmp_out:
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            writer = cv2.VideoWriter(tmp_out, fourcc, fps, (w, h))
            
            frame_idx = 0
            success, frame = cap.read()
            
            while success:
                if frame_idx > 0 and frame_idx-1 < len(transforms_smooth):
                    dx, dy, da = transforms_smooth[frame_idx-1]
                    m = np.array([
                        [np.cos(da), -np.sin(da), dx],
                        [np.sin(da), np.cos(da), dy]
                    ])
                    frame_stabilized = cv2.warpAffine(
                        frame, m, (w, h), 
                        borderMode=cv2.BORDER_REFLECT
                    )
                else:
                    frame_stabilized = frame
                    
                writer.write(frame_stabilized)
                success, frame = cap.read()
                frame_idx += 1
                
            writer.release()
            cap.release()
            
            # Remux dengan FFmpeg untuk kompatibilitas lebih baik
            run_ffmpeg([
                '-i', tmp_out, 
                '-c:v', 'libx264', 
                '-preset', 'fast', 
                '-crf', '18', 
                '-c:a', 'aac', 
                '-b:a', '128k', 
                outfile
            ])
            
        return outfile

# ---------- Editor ----------

class Editor:
    """Kelas untuk operasi editing video"""
    
    def __init__(self):
        self.supported_filters = {
            'grayscale': 'format=gray',
            'hflip': 'hflip',
            'vflip': 'vflip',
            'noise': 'noise=alls=20:allf=t',
            'unsharp': 'unsharp=5:5:1.0:5:5:0.0'
        }
        logger.debug("Video Editor initialized")

    def trim(self, infile: str, start: float, end: float, outfile: str) -> str:
        """Potong video dari start ke end (dalam detik)"""
        logger.info("Trimming video: %s [%s-%s] -> %s", infile, start, end, outfile)
        
        if _MOVIEPY_AVAILABLE:
            try:
                clip = VideoFileClip(infile).subclip(start, end)
                clip.write_videofile(
                    outfile, 
                    codec='libx264', 
                    audio_codec='aac',
                    logger=None  # Nonaktifkan logger moviepy
                )
                return outfile
            except Exception as e:
                logger.warning("MoviePy failed, falling back to FFmpeg: %s", e)
                
        # Fallback ke FFmpeg
        run_ffmpeg([
            '-i', infile, 
            '-ss', str(start), 
            '-to', str(end), 
            '-c', 'copy', 
            outfile
        ])
        return outfile

    def concat(self, files: List[str], outfile: str) -> str:
        """Gabungkan multiple video files"""
        logger.info("Concatenating %d files -> %s", len(files), outfile)
        
        if _MOVIEPY_AVAILABLE:
            try:
                clips = [VideoFileClip(f) for f in files]
                final = concatenate_videoclips(clips, method='compose')
                final.write_videofile(
                    outfile, 
                    codec='libx264', 
                    audio_codec='aac',
                    logger=None
                )
                return outfile
            except Exception as e:
                logger.warning("MoviePy failed, falling back to FFmpeg: %s", e)
                
        # Fallback ke FFmpeg
        with temporary_video_file(mode='w', suffix='.txt') as list_file:
            for f in files:
                list_file.write(f"file '{os.path.abspath(f)}'\n")
            list_file.flush()
            
            run_ffmpeg([
                '-f', 'concat', 
                '-safe', '0', 
                '-i', list_file, 
                '-c', 'copy', 
                outfile
            ])
            
        return outfile

    def speed(self, infile: str, factor: float, outfile: str) -> str:
        """Ubah kecepatan video"""
        logger.info("Changing speed: %s x%s -> %s", infile, factor, outfile)
        
        run_ffmpeg([
            '-i', infile, 
            '-filter_complex', 
            f"[0:v]setpts={1/float(factor)}*PTS[v];[0:a]atempo={float(factor)}[a]", 
            '-map', '[v]', 
            '-map', '[a]', 
            outfile
        ])
        return outfile

    def apply_filter(self, infile: str, filter_name: str, outfile: str) -> str:
        """Terapkan filter FFmpeg ke video"""
        logger.info("Applying filter %s to %s -> %s", filter_name, infile, outfile)
        
        if filter_name not in self.supported_filters:
            raise ValueError(f"Filter not supported: {filter_name}")
            
        filter_str = self.supported_filters[filter_name]
        run_ffmpeg([
            '-i', infile, 
            '-vf', filter_str, 
            '-c:a', 'copy', 
            outfile
        ])
        return outfile

# ---------- Background Job Processing ----------

# Queue untuk background jobs
job_queue = Queue()
job_processor_thread = None

def job_processor():
    """Background worker untuk memproses jobs"""
    logger.info("Job processor started")
    
    while True:
        job_id, action, data = job_queue.get()
        logger.info("Processing job %d: %s", job_id, action)
        
        try:
            with get_db_cursor() as cur:
                cur.execute(
                    'SELECT filename, user_id FROM jobs WHERE id=?', 
                    (job_id,)
                )
                job = cur.fetchone()
                
                if not job:
                    logger.error("Job %d not found", job_id)
                    continue
                    
                infile = job['filename']
                outfile = infile + '.processed.mp4'
                
                # Update status to processing
                cur.execute(
                    'UPDATE jobs SET status=?, updated_at=? WHERE id=?', 
                    ('processing', datetime.utcnow().isoformat(), job_id)
                )
                
                # Process based on action
                if action == 'stabilize':
                    s = Stabilizer(smoothing_window=data.get('smoothing', 30))
                    s.stabilize(infile, outfile)
                elif action == 'trim':
                    e = Editor()
                    e.trim(infile, data.get('start', 0), data.get('end', 10), outfile)
                elif action == 'concat':
                    e = Editor()
                    files = data.get('files', [])
                    e.concat(files, outfile)
                elif action == 'speed':
                    e = Editor()
                    e.speed(infile, data.get('factor', 1.0), outfile)
                elif action == 'filter':
                    e = Editor()
                    e.apply_filter(infile, data.get('filter_name', 'grayscale'), outfile)
                else:
                    raise ValueError(f"Unknown action: {action}")
                
                # Update status to done
                cur.execute(
                    'UPDATE jobs SET status=?, result=?, updated_at=? WHERE id=?', 
                    ('done', outfile, datetime.utcnow().isoformat(), job_id)
                )
                logger.info("Job %d completed successfully", job_id)
                
        except Exception as e:
            logger.error("Job %d failed: %s", job_id, str(e))
            with get_db_cursor() as cur:
                cur.execute(
                    'UPDATE jobs SET status=?, result=?, updated_at=? WHERE id=?', 
                    ('error', str(e), datetime.utcnow().isoformat(), job_id)
                )
        finally:
            job_queue.task_done()

def start_job_processor():
    """Start background job processor"""
    global job_processor_thread
    if job_processor_thread is None or not job_processor_thread.is_alive():
        job_processor_thread = threading.Thread(target=job_processor, daemon=True)
        job_processor_thread.start()
        logger.info("Job processor started")

# ---------- Server (Flask) ----------

def create_app():
    """Buat dan konfigurasi Flask app"""
    if not _FLASK_AVAILABLE:
        raise RuntimeError('Flask not installed. Run: pip install flask')
        
    app = Flask('pycine_server')
    app.config['UPLOAD_FOLDER'] = STORAGE_DIR
    app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max file size
    
    # Simple rate limiting storage
    request_timestamps = {}
    
    def check_rate_limit(ip, max_requests=5, window_seconds=60):
        """Simple in-memory rate limiting"""
        now = time.time()
        if ip not in request_timestamps:
            request_timestamps[ip] = []
            
        # Hapus request lama
        request_timestamps[ip] = [
            t for t in request_timestamps[ip] 
            if now - t < window_seconds
        ]
        
        if len(request_timestamps[ip]) >= max_requests:
            return False
            
        request_timestamps[ip].append(now)
        return True
        
    def rate_limited(f):
        """Decorator untuk rate limiting"""
        @wraps(f)
        def decorated_function(*args, **kwargs):
            ip = request.remote_addr
            if not check_rate_limit(ip):
                return jsonify({'error': 'Rate limit exceeded'}), 429
            return f(*args, **kwargs)
        return decorated_function
        
    def auth_required(f):
        """Decorator untuk autentikasi JWT"""
        @wraps(f)
        def decorated_function(*args, **kwargs):
            auth_header = request.headers.get('Authorization')
            if not auth_header or not auth_header.startswith('Bearer '):
                return jsonify({'error': 'Authorization header missing or invalid'}), 401
                
            token = auth_header[7:]  # Remove 'Bearer ' prefix
            
            try:
                if _PYJWT_AVAILABLE:
                    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
                    g.user_id = payload.get('user_id')
                    g.is_premium = payload.get('is_premium', False)
                else:
                    # Fallback ke license check
                    if not check_license(token):
                        return jsonify({'error': 'Invalid license'}), 401
                    g.user_id = get_user_from_license(token)
                    g.is_premium = True
                    
            except jwt.ExpiredSignatureError:
                return jsonify({'error': 'Token expired'}), 401
            except jwt.InvalidTokenError:
                return jsonify({'error': 'Invalid token'}), 401
            except Exception as e:
                return jsonify({'error': f'Authentication error: {str(e)}'}), 401
                
            return f(*args, **kwargs)
        return decorated_function
        
    @app.route('/register', methods=['POST'])
    @rate_limited
    def register():
        """Endpoint untuk registrasi user baru"""
        data = request.json or {}
        username = data.get('username')
        password = data.get('password')
        
        if not username or not password:
            return jsonify({'error': 'Username and password required'}), 400
            
        if len(password) < 8:
            return jsonify({'error': 'Password must be at least 8 characters'}), 400
            
        try:
            create_user(username, password, is_premium=False)
            return jsonify({'status': 'User created successfully'})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            logger.error("Registration error: %s", str(e))
            return jsonify({'error': 'Internal server error'}), 500
            
    @app.route('/token', methods=['POST'])
    @rate_limited
    def get_token():
        """Endpoint untuk mendapatkan JWT token"""
        data = request.json or {}
        username = data.get('username')
        password = data.get('password')
        
        user_info = verify_user(username, password)
        if not user_info:
            return jsonify({'error': 'Invalid credentials'}), 401
            
        user_id, is_premium = user_info
        
        if _PYJWT_AVAILABLE:
            payload = {
                'user_id': user_id,
                'is_premium': is_premium,
                'exp': datetime.utcnow() + timedelta(hours=12)
            }
            token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)
        else:
            # Fallback: cari license key user
            with get_db_cursor() as cur:
                cur.execute(
                    'SELECT key FROM licenses WHERE user_id=? AND expires_at > ?', 
                    (user_id, datetime.utcnow().isoformat())
                )
                license_row = cur.fetchone()
                if license_row:
                    token = license_row['key']
                else:
                    return jsonify({'error': 'No valid license found'}), 401
                    
        return jsonify({
            'token': token,
            'user_id': user_id,
            'is_premium': is_premium
        })
        
    @app.route('/upload', methods=['POST'])
    @auth_required
    @rate_limited
    def upload_file():
        """Endpoint untuk upload file video"""
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
            
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
            
        if not allowed_file(file.filename):
            return jsonify({'error': 'File type not allowed'}), 400
            
        # Validasi bahwa file benar-benar video
        file_content = file.stream.read(1024)
        file.stream.seek(0)
        
        try:
            file_type = magic.from_buffer(file_content, mime=True)
            if not file_type.startswith('video/'):
                return jsonify({'error': 'Uploaded file is not a video'}), 400
        except Exception:
            # Jika magic tidak tersedia, lewati validasi ini
            pass
            
        # Simpan file
        filename = f"{int(time.time())}_{secrets.token_hex(8)}_{file.filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        # Buat job record
        with get_db_cursor() as cur:
            cur.execute(
                'INSERT INTO jobs (user_id, filename, status, created_at, updated_at) VALUES (?,?,?,?,?)',
                (g.user_id, filepath, 'queued', datetime.utcnow().isoformat(), datetime.utcnow().isoformat())
            )
            job_id = cur.lastrowid
            
        return jsonify({
            'job_id': job_id,
            'filename': filename,
            'status': 'queued'
        })
        
    @app.route('/jobs/<int:job_id>', methods=['GET'])
    @auth_required
    def get_job_status(job_id):
        """Endpoint untuk mendapatkan status job"""
        with get_db_cursor() as cur:
            cur.execute(
                'SELECT id, filename, status, result, created_at, updated_at FROM jobs WHERE id=? AND user_id=?',
                (job_id, g.user_id)
            )
            job = cur.fetchone()
            
        if not job:
            return jsonify({'error': 'Job not found'}), 404
            
        return jsonify({
            'id': job['id'],
            'filename': job['filename'],
            'status': job['status'],
            'result': job['result'],
            'created_at': job['created_at'],
            'updated_at': job['updated_at']
        })
        
    @app.route('/jobs/<int:job_id>/process', methods=['POST'])
    @auth_required
    def process_job(job_id):
        """Endpoint untuk memproses job"""
        data = request.json or {}
        action = data.get('action')
        
        if not action:
            return jsonify({'error': 'Action required'}), 400
            
        # Validasi akses ke job
        with get_db_cursor() as cur:
            cur.execute(
                'SELECT status FROM jobs WHERE id=? AND user_id=?',
                (job_id, g.user_id)
            )
            job = cur.fetchone()
            
        if not job:
            return jsonify({'error': 'Job not found'}), 404
            
        if job['status'] != 'queued':
            return jsonify({'error': 'Job already processed'}), 400
            
        # Premium feature check
        premium_actions = ['stabilize', 'concat', 'filter']
        if action in premium_actions and not g.is_premium:
            return jsonify({'error': 'Premium feature requires license'}), 403
            
        # Tambahkan job ke queue
        job_queue.put((job_id, action, data))
        
        return jsonify({
            'status': 'processing',
            'message': 'Job queued for processing'
        })
        
    @app.route('/jobs/<int:job_id>/download', methods=['GET'])
    @auth_required
    def download_job_result(job_id):
        """Endpoint untuk mendownload hasil job"""
        with get_db_cursor() as cur:
            cur.execute(
                'SELECT result, status FROM jobs WHERE id=? AND user_id=?',
                (job_id, g.user_id)
            )
            job = cur.fetchone()
            
        if not job:
            return jsonify({'error': 'Job not found'}), 404
            
        if job['status'] != 'done' or not job['result']:
            return jsonify({'error': 'Job not completed'}), 400
            
        if not os.path.exists(job['result']):
            return jsonify({'error': 'Result file not found'}), 404
            
        return send_file(
            job['result'],
            as_attachment=True,
            download_name=os.path.basename(job['result'])
        )
        
    @app.errorhandler(413)
    def too_large(e):
        return jsonify({'error': 'File too large'}), 413
        
    @app.errorhandler(500)
    def internal_error(e):
        logger.error("Server error: %s", str(e))
        return jsonify({'error': 'Internal server error'}), 500
        
    return app

# ---------- CLI ----------

def main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(prog='pycine', description='PyCine - Video editor & stabilizer')
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # init command
    parser_init = subparsers.add_parser('init', help='Initialize local DB and storage')
    parser_init.set_defaults(func=lambda args: init_db())
    
    # create-user command
    parser_user = subparsers.add_parser('create-user', help='Create a local user')
    parser_user.add_argument('username', help='Username')
    parser_user.add_argument('password', help='Password')
    parser_user.add_argument('--premium', action='store_true', help='Premium user')
    parser_user.set_defaults(func=lambda args: create_user(args.username, args.password, args.premium))
    
    # create-license command
    parser_license = subparsers.add_parser('create-license', help='Create license for user')
    parser_license.add_argument('user_id', type=int, help='User ID')
    parser_license.add_argument('--days', type=int, default=30, help='License validity in days')
    parser_license.set_defaults(func=lambda args: print(create_license_for_user(args.user_id, args.days)))
    
    # stabilize command
    parser_stab = subparsers.add_parser('stabilize', help='Stabilize video file')
    parser_stab.add_argument('input', help='Input video file')
    parser_stab.add_argument('-o', '--output', default='stabilized.mp4', help='Output video file')
    parser_stab.add_argument('--smoothing', type=int, default=30, help='Smoothing window size')
    parser_stab.set_defaults(func=lambda args: Stabilizer(args.smoothing).stabilize(args.input, args.output))
    
    # trim command
    parser_trim = subparsers.add_parser('trim', help='Trim video')
    parser_trim.add_argument('input', help='Input video file')
    parser_trim.add_argument('start', type=float, help='Start time (seconds)')
    parser_trim.add_argument('end', type=float, help='End time (seconds)')
    parser_trim.add_argument('-o', '--output', default='trimmed.mp4', help='Output video file')
    parser_trim.set_defaults(func=lambda args: Editor().trim(args.input, args.start, args.end, args.output))
    
    # concat command
    parser_concat = subparsers.add_parser('concat', help='Concatenate files')
    parser_concat.add_argument('files', nargs='+', help='Files to concatenate')
    parser_concat.add_argument('-o', '--output', default='concat.mp4', help='Output video file')
    parser_concat.set_defaults(func=lambda args: Editor().concat(args.files, args.output))
    
    # speed command
    parser_speed = subparsers.add_parser('speed', help='Change video speed')
    parser_speed.add_argument('input', help='Input video file')
    parser_speed.add_argument('factor', type=float, help='Speed factor')
    parser_speed.add_argument('-o', '--output', default='speed.mp4', help='Output video file')
    parser_speed.set_defaults(func=lambda args: Editor().speed(args.input, args.factor, args.output))
    
    # filter command
    parser_filter = subparsers.add_parser('filter', help='Apply filter to video')
    parser_filter.add_argument('input', help='Input video file')
    parser_filter.add_argument('filter_name', help='Filter name', 
                              choices=['grayscale', 'hflip', 'vflip', 'noise', 'unsharp'])
    parser_filter.add_argument('-o', '--output', default='filtered.mp4', help='Output video file')
    parser_filter.set_defaults(func=lambda args: Editor().apply_filter(args.input, args.filter_name, args.output))
    
    # serve command
    parser_serve = subparsers.add_parser('serve', help='Run HTTP server')
    parser_serve.add_argument('--host', default='127.0.0.1', help='Host to bind to')
    parser_serve.add_argument('--port', type=int, default=8080, help='Port to listen on')
    parser_serve.add_argument('--debug', action='store_true', help='Enable debug mode')
    parser_serve.set_defaults(func=lambda args: create_app().run(
        host=args.host, port=args.port, debug=args.debug, threaded=True))
    
    # Parse args
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
        
    # Start job processor untuk command serve
    if args.command == 'serve':
        start_job_processor()
        
    try:
        result = args.func(args)
        if result is not None:
            print(result)
    except Exception as e:
        logger.error("Error executing command: %s", str(e))
        sys.exit(1)

if __name__ == '__main__':
    # Inisialisasi database jika belum ada
    if not os.path.exists(DB_PATH):
        init_db()
        
    # Start job processor
    start_job_processor()
    
    # Jalankan aplikasi
    main()