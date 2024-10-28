import os
import json
import tempfile
import hashlib
import re
import threading
from urllib.parse import urlparse, quote
import requests
from flask import *
from flask_cors import CORS
from yt_dlp import YoutubeDL

# Set up Flask app
app = Flask(__name__)
CORS(app)

# Set up global variables
CACHE_DIR = tempfile.gettempdir()
cookieFile = './cookies.txt'
loginCreds = {}

# Load login credentials if available
def load_login_creds(filename='login_creds.json'):
    global loginCreds
    try:
        with open(filename, 'r') as file:
            loginCreds = json.load(file)
    except FileNotFoundError:
        print("Login credentials file not found, proceeding without login.")

load_login_creds()

def getMetadata(url):
    domain = urlparse(url).netloc
    loginCred = loginCreds.get(domain)

    ydl_opts = {
        "cookiefile": cookieFile,
        "format": 'bestvideo+bestaudio/best',
        "skip_download": True,
        "verbose": True,
        'geo_bypass': True,
        'geo_bypass_country': 'CA',
        'ratelimit': 10 * 1024 * 1024,
        "nocheckcertificate": True,
        "http_header": {
            "User-Agent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/85.0.4183.83 Safari/537.36',
            'Referer': f'https://{domain}',
        }
    }

    if loginCred:
        ydl_opts['username'] = loginCred['username']
        ydl_opts['password'] = loginCred['password']

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info
    except Exception as e:
        return {"error": f"Error while fetching video info: {e}"}

# Video streaming and caching functions
def stream_video(url, user_agent, video_title):
    domain = urlparse(url).netloc
    headers = {
        'User-Agent': user_agent,
        'Referer': f'https://{domain}',
        'Connection': 'keep-alive'
    }

    # Generate cache filename and paths
    cache_filename = hashlib.md5(url.encode()).hexdigest() + ".cache"
    cache_path = os.path.join(CACHE_DIR, cache_filename)
    complete_marker = cache_path + ".complete"

    # Start background download if cache is incomplete
    if not os.path.exists(complete_marker):
        threading.Thread(target=download_full_video, args=(url, headers, cache_path, complete_marker)).start()

    # Serve from cache if complete, otherwise stream and cache in real-time
    if os.path.exists(complete_marker):
        print("Serving from cache")
        return stream_from_cache(cache_path, video_title)
    else:
        return stream_and_cache(url, headers, cache_path, video_title)

def download_full_video(url, headers, cache_path, complete_marker):
    try:
        response = requests.head(url, headers=headers, timeout=10)
        file_size = int(response.headers.get('Content-Length', 0))

        if file_size == 0:
            print("Unable to fetch video size.")
            return

        # Download file in one go for Kaggle compatibility
        with open(cache_path, 'wb') as cache_file:
            response = requests.get(url, headers=headers, stream=True)
            for chunk in response.iter_content(chunk_size=2 * 1024 * 1024):
                if chunk:
                    cache_file.write(chunk)

        # Mark cache as complete
        with open(complete_marker, 'w') as marker_file:
            marker_file.write("Complete")
    except requests.RequestException as e:
        print(f"Error caching video: {e}")

def stream_and_cache(url, headers, cache_path, video_title):
    try:
        response = requests.get(url, headers=headers, stream=True, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        return f"Error fetching video: {e}", 500

    encoded_title = quote(f"{video_title}.mp4")
    response_headers = {
        'Content-Type': response.headers.get('Content-Type', 'video/mp4'),
        'Content-Disposition': f'inline; filename="{encoded_title}"',
        'Accept-Ranges': 'bytes'
    }

    def generate_and_cache():
        with open(cache_path, 'wb') as cache_file:
            for chunk in response.iter_content(chunk_size=1024 * 512):
                if chunk:
                    cache_file.write(chunk)
                    yield chunk
            cache_file.flush()

    return Response(generate_and_cache(), headers=response_headers)

def stream_from_cache(cache_path, video_title):
    encoded_title = quote(f"{video_title}.mp4")
    file_size = os.path.getsize(cache_path)

    response_headers = {
        'Content-Type': 'video/mp4',
        'Content-Disposition': f'inline; filename="{encoded_title}"',
        'Content-Length': str(file_size),
        'Accept-Ranges': 'bytes'
    }

    range_header = request.headers.get('Range')
    if range_header:
        byte1, byte2 = 0, None
        match = re.search(r'(\d+)-(\d*)', range_header)
        if match:
            byte1, byte2 = map(lambda x: int(x) if x else None, match.groups())
        start = byte1
        end = byte2 if byte2 is not None else file_size - 1
        length = end - start + 1
        response_headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        response_headers['Content-Length'] = str(length)

        def generate():
            with open(cache_path, 'rb') as cache_file:
                cache_file.seek(start)
                while start <= end:
                    chunk = cache_file.read(min(1024 * 512, end - start + 1))
                    if not chunk:
                        break
                    start += len(chunk)
                    yield chunk

        return Response(generate(), headers=response_headers, status=206)

    return Response(generate_full_file(cache_path), headers=response_headers)

# Helper function to stream the entire file if no Range header is present
def generate_full_file(cache_path):
    with open(cache_path, 'rb') as cache_file:
        while True:
            chunk = cache_file.read(1024 * 512)
            if not chunk:
                break
            yield chunk

# Flask routes
@app.route('/')
def main():
    return render_template('index.html')

@app.route('/v', methods=['POST'])
def info():
    data = request.get_json()
    url = data.get('url')
    
    if not url:
        return {'error': "Please enter url"}, 400
    
    info = getMetadata(url)
    
    if "error" in info:
        return jsonify({'error': info['error']}), 400
    
    return jsonify(info)

@app.route('/preview')
def preview():
    url = request.args.get('video_url')
    user_agent = request.headers.get('User-Agent', 'default-user-agent')
    video_title = request.args.get('filename', 'video')
    return stream_video(url, user_agent, video_title)

# Run the app
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)