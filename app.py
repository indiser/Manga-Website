from flask import Flask, g, request, jsonify, abort, request, render_template
import json
import re
from curl_cffi import requests as tls_requests
# import requests
from datetime import datetime
from bs4 import BeautifulSoup
from flask_cors import CORS
import os
import shutil
import img2pdf
from PIL import Image
from flask import send_file
import time
import threading
import io
import urllib.parse
import json
import itertools


app=Flask(__name__)
app.json.sort_keys = False
CORS(app)

session = tls_requests.Session(impersonate="chrome")

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api")
def getmanga():
    manga_id=request.args.get("manga_id")

    if not manga_id:
        return jsonify(error="Missing manga_id parameter"), 400
    nhentai_url=f"https://nhentai.net/g/{manga_id}/"
    
    response=session.get(nhentai_url)

    if response.status_code==404:
        return jsonify(error="Not Found"), 404
    
    if response.status_code==429:
        return jsonify(error="Too Many Requests"), 429
    
    if response.status_code != 200:
        return jsonify(error=f"Unexpected Server Error: {response.status_code}"), response.status_code
    
    if response.status_code==200:
        data_payload = {}
        json_pattern = r'<script type="application/json" data-sveltekit-fetched data-url="/api/v2/galleries/.*?">(.*?)</script>'
        match = re.search(json_pattern, response.text, re.DOTALL)
        if match:
            clean_json_str = match.group(1)
            svelte_wrapper = json.loads(clean_json_str)
            gallery_data = json.loads(svelte_wrapper.get('body', '{}'))

            media_id = gallery_data.get('media_id')
            pages = gallery_data.get('pages', [])
            page_urls = []

            for page in pages:
                path = page.get('path')
                if path:
                    page_urls.append(f"https://i.nhentai.net/{path}")

            tags = gallery_data.get('tags', [])
            data_payload = {
                'id': gallery_data.get('id'),
                'title': gallery_data.get('title', {}).get('english'),
                'date': datetime.fromtimestamp(gallery_data.get('upload_date', 0)).strftime('%Y-%m-%d') if gallery_data.get('upload_date') else None,
                'parodies': [tag.get('name') for tag in tags if tag.get('type') == 'parody'],
                'characters': [tag.get('name') for tag in tags if tag.get('type') == 'character'],
                'groups': [tag.get('name') for tag in tags if tag.get('type') == 'group'],
                'categories': [tag.get('name') for tag in tags if tag.get('type') == 'category'],
                'language': [tag.get('name') for tag in tags if tag.get('type') == 'language'],
                'favorites': gallery_data.get('num_favorites', 0),
                'tags': [tag.get('name') for tag in tags if tag.get('type') == 'tag'],
                'artists': [tag.get('name') for tag in tags if tag.get('type') == 'artist'],
                'num_pages': gallery_data.get('num_pages', 0),
                'media_id': media_id,
                'page_urls': page_urls
            }
        
        soup = BeautifulSoup(response.text, 'html.parser')
        recommendations = []
        related_container = soup.find('div', id='related-container')

        if related_container:
            galleries = related_container.find_all('div', class_='gallery')
            for gallery in galleries:
                link_tag = gallery.find('a', class_='cover')
                if not link_tag:
                    continue
                    
                href = link_tag.get('href', '')
                rec_id = href.strip('/').split('/')[-1] 
                
                caption_tag = gallery.find('div', class_='caption')
                rec_title = caption_tag.text.strip() if caption_tag else "Unknown Title"
                
                img_tag = gallery.find('img')
                thumbnail_url = None
                if img_tag:
                    thumbnail_url = img_tag.get('data-src') or img_tag.get('src')
                    if thumbnail_url and thumbnail_url.startswith('//'):
                        thumbnail_url = f"https:{thumbnail_url}"


                css_classes = gallery.get('class', [])
                language = 'japanese' # Default
                
                if 'lang-gb' in css_classes:
                    language = 'english'
                elif 'lang-cn' in css_classes:
                    language = 'chinese'
                elif 'lang-jp' in css_classes:
                    language = 'japanese'

                if not language:
                    title_lower = rec_title.lower()
                    if '[english]' in title_lower:
                        language = 'english'
                    elif '[chinese]' in title_lower or '翻译' in title_lower:
                        language = 'chinese'
                    else:
                        language = 'japanese'
                
                if rec_id.isdigit():
                    recommendations.append({
                        'id': int(rec_id),
                        'title': rec_title,
                        'thumbnail_image': thumbnail_url,
                        "language":language
                    })

        data_payload['recommendations'] = recommendations

        cover_div = soup.find('div', id='cover')
        if cover_div:
            cover_img = cover_div.find('img')
            if cover_img and 'src' in cover_img.attrs:
                data_payload['cover_image'] = cover_img.get('data-src') or cover_img.get('src')

        return data_payload
        
    return jsonify(data_payload)
    

@app.route("/api/download")
def download_manga():
    manga_id = request.args.get("manga_id")
    if not manga_id:
        return jsonify(error="Missing manga_id"), 400

    # 1. Fetch metadata to get media_id and page count
    nhentai_url = f"https://nhentai.net/g/{manga_id}/"
    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    response = session.get(nhentai_url)
    
    if response.status_code != 200:
        return jsonify(error="Failed to fetch manga data"), response.status_code

    match = re.search(r'<script type="application/json" data-sveltekit-fetched data-url="/api/v2/galleries/.*?">(.*?)</script>', response.text)
    if not match:
        return jsonify(error="Metadata not found"), 500
    
    clean_json_str = match.group(1)
    svelte_wrapper = json.loads(clean_json_str)
    gallery_data = json.loads(svelte_wrapper.get('body', '{}'))
    media_id = gallery_data.get('media_id')
    safe_title = re.sub(r'[<>:"/\\|?*]', '', gallery_data['title']['english'])[:50]
    
    # 2. Setup temporary folders with absolute paths
    base_dir = os.path.abspath(os.path.dirname(__file__))
    temp_folder = os.path.join(base_dir, f"TEMP_{manga_id}")
    os.makedirs(temp_folder, exist_ok=True)
    pdf_filename = os.path.join(base_dir, f"{manga_id}_{safe_title}.pdf")
    
    image_paths = []
    
    # 3. Download and Optimize Images (WARNING: This will block the server until finished)
    for i, page in enumerate(gallery_data.get('pages',[]), start=1):
        path = page.get('path','jpg')
        if not path:
            continue
        img_url=f"https://i.nhentai.net/{path}"
        filepath = os.path.join(temp_folder, f"{i}.webp")
        try:
            # 1. Download the raw file in one shot using the Cloudflare bypass
            img_response = session.get(img_url)
            
            if img_response.status_code == 200:
                with open(filepath, 'wb') as f:
                    f.write(img_response.content)
            else:
                print(f"Error: Status {img_response.status_code} for {img_url}")
                continue
            
            # 2. MINIMAL FIX: Strip Alpha and force JPEG format
            jpg_path = os.path.join(temp_folder, f"{i}.jpg")
            with Image.open(filepath) as img:
                if img.mode in ("RGBA", "P", "LA"):
                    img = img.convert("RGB") 
                img.save(jpg_path, "JPEG")
            
            # 3. Append only the guaranteed safe JPEG to the PDF compiler
            image_paths.append(jpg_path)
            
        except Exception as e:
            print(f"Error processing page {i}: {e}")
            continue

    # 4. Generate PDF
    if not image_paths:
        shutil.rmtree(temp_folder, ignore_errors=True)
        return jsonify(error="Failed to download any images"), 500

    try:
        with open(pdf_filename, "wb") as f:
            f.write(img2pdf.convert(image_paths))
    except Exception as e:
        shutil.rmtree(temp_folder)
        return jsonify(error=f"PDF generation failed: {str(e)}"), 500

    # 5. Cleanup temp images
    shutil.rmtree(temp_folder, ignore_errors=True)

    # 6. Send file and schedule deletion
    def delete_file():
        time.sleep(2)
        try:   
            if os.path.exists(pdf_filename):
                os.remove(pdf_filename)
        except Exception as e:
            print(f"Error: {e}")
    
    threading.Thread(target=delete_file, daemon=True).start()
    return send_file(pdf_filename, as_attachment=True, download_name=f"{safe_title}.pdf")

@app.route("/api/download_page")
def download_page():
    image_url = request.args.get("url")
    custom_filename = request.args.get("filename") # Catch the new parameter
    
    if not image_url or "nhentai.net" not in image_url:
        return jsonify(error="Invalid or unauthorized URL"), 400
    
    if "galleries/" in image_url:
        parts = image_url.split("galleries/")
        if len(parts) > 2:
            # Reconstruct the URL properly
            image_url = f"{parts[0]}galleries/{parts[-1]}"

    try:
        response = session.get(image_url)
        
        if response.status_code != 200:
            return jsonify(error=f"Failed to fetch image. Status: {response.status_code}"), response.status_code

        raw_image_data = io.BytesIO(response.content)
        img = Image.open(raw_image_data)
        
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        png_buffer = io.BytesIO()
        img.save(png_buffer, format="PNG")
        png_buffer.seek(0)

        # Apply the Custom Naming Logic Safely
        if custom_filename:
            # Server-side sanitization: Strip out any dangerous path characters
            safe_filename = re.sub(r'[\\/*?:"<>|]', "", custom_filename)
            # Ensure it strictly ends with .png
            if not safe_filename.lower().endswith('.png'):
                safe_filename += '.png'
            final_name = safe_filename
        else:
            # Fallback if no custom name is provided
            original_filename = image_url.split("/")[-1]
            base_name = original_filename.rsplit('.', 1)[0]
            final_name = f"{base_name}.png"

        return send_file(
            png_buffer,
            mimetype='image/png',
            as_attachment=True,
            download_name=final_name
        )
    except Exception as e:
        return jsonify(error=f"Internal server error: {str(e)}"), 500
    
@app.route("/api/homepage")
def get_homepage():
    page = request.args.get("page", 1, type=int)
    try:
        # url = "https://nhentai.net/"
        url = f"https://nhentai.net/?page={page}"
        # We must use tls_requests to bypass Cloudflare on the homepage
        response = session.get(url)
        
        if response.status_code != 200:
            return jsonify(error="Failed to fetch discovery feed"), response.status_code

        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Target the main gallery containers
        galleries = soup.find_all('div', class_='gallery')
        results = []
        
        # Limit to 18 items so the frontend grid looks clean and balanced
        for gallery in galleries[:18]:
            link_tag = gallery.find('a', class_='cover')
            caption_tag = gallery.find('div', class_='caption')
            img_tag = gallery.find('img')
            
            if link_tag and caption_tag and img_tag:
                manga_id = link_tag['href'].strip('/').split('/')[-1]
                title = caption_tag.text

                css_classes = gallery.get('class', [])
                
                language = 'japanese' # Default fallback
                
                if 'lang-gb' in css_classes:
                    language = 'english'
                elif 'lang-cn' in css_classes:
                    language = 'chinese'
                elif 'lang-jp' in css_classes:
                    language = 'japanese'

                # 2. Secondary Failsafe: If the uploader forgot the tag, aggressively parse the title
                if not language:
                    title_lower = title.lower()
                    if '[english]' in title_lower:
                        language = 'english'
                    elif '[chinese]' in title_lower or '翻译' in title_lower:
                        language = 'chinese'
                    else:
                        language = 'japanese' # The ultimate fallback

                # Nhentai lazy-loads homepage images. We must grab 'data-src' if it exists.
                cover_url = img_tag.get('data-src') or img_tag.get('src')
                if cover_url and cover_url.startswith("//"):
                    cover_url = "https:" + cover_url
                    
                results.append({
                    'id': manga_id,
                    'title': title,
                    'cover_image': cover_url,
                    'language':language
                })
                
        return jsonify(results)
    except Exception as e:
        return jsonify(error=f"Internal Server Error: {str(e)}"), 500

    
@app.route("/api/search")
def search_manga():
    query = request.args.get("q")
    page = request.args.get("page", 1, type=int)

    if not query:
        return jsonify(error="Missing search query"), 400

    # Build the exact search URL nHentai uses
    # url = f"https://nhentai.net/search/?q={query}"
    url = f"https://nhentai.net/search/?q={query}&page={page}"
    
    try:
        response = session.get(url)
        
        if response.status_code != 200:
            return jsonify(error=f"Upstream Error: {response.status_code}"), response.status_code

        soup = BeautifulSoup(response.text, 'html.parser')
        galleries = soup.find_all('div', class_='gallery')
        results = []
        
        for gallery in galleries:
            link_tag = gallery.find('a', class_='cover')
            caption_tag = gallery.find('div', class_='caption')
            img_tag = gallery.find('img')
            
            if link_tag and caption_tag and img_tag:
                manga_id = link_tag['href'].strip('/').split('/')[-1]
                title = caption_tag.text.strip()
                
                css_classes = gallery.get('class', [])
                
                language = 'japanese' # Default fallback
                
                if 'lang-gb' in css_classes:
                    language = 'english'
                elif 'lang-cn' in css_classes:
                    language = 'chinese'
                elif 'lang-jp' in css_classes:
                    language = 'japanese'

                # 2. Secondary Failsafe: If the uploader forgot the tag, aggressively parse the title
                if not language:
                    title_lower = title.lower()
                    if '[english]' in title_lower:
                        language = 'english'
                    elif '[chinese]' in title_lower or '翻译' in title_lower:
                        language = 'chinese'
                    else:
                        language = 'japanese' # The ultimate fallback


                # Handle lazy loading
                cover_url = img_tag.get('data-src') or img_tag.get('src')
                if cover_url and cover_url.startswith("//"):
                    cover_url = "https:" + cover_url
                    
                results.append({
                    'id': manga_id,
                    'title': title,
                    'cover_image': cover_url,
                    'language':language
                })
                
        return jsonify(results)
    except Exception as e:
        return jsonify(error=f"Search failed: {str(e)}"), 500


if __name__=='__main__':
    app.run(debug=True)
