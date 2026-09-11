import requests
import re
import json
import pandas as pd
import urllib3
import folium
from flask import Flask
import webbrowser
import threading
import math
import traceback
import time
from datetime import datetime
from collections import deque 

import numpy as np
import matplotlib
matplotlib.use('Agg') 
import matplotlib.pyplot as plt
from matplotlib import colors
from matplotlib.path import Path as MplPath 
import base64
from io import BytesIO
from scipy.spatial.distance import cdist

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIEMPO_REFRESCO = 3600 
PUERTO = 5003 

app = Flask(__name__)

# Buffer circular para manejar sesiones largas
HISTORY_BUFFER = deque(maxlen=12) 

CACHE_HTML = None        
LAST_UPDATE = 0          
CACHE_MASK = None 


def redondear_segun_norma(valor):
    try:
        decimal, _ = math.modf(valor)
        return int(math.ceil(valor)) if decimal >= 0.5 else int(math.floor(valor))
    except:
        return int(valor)

def calcular_categoria_estacion(pm25, pm10):
    # Aplicar categorización por nivel de índice de calidad del aire
    def get_cat(v, limites):
        if v is None or pd.isna(v): return 0
        val = redondear_segun_norma(v)
        if val <= limites[0]: return 1
        if val <= limites[1]: return 2
        if val <= limites[2]: return 3
        if val <= limites[3]: return 4
        return 5

    c25 = get_cat(pm25, [15, 25, 79, 130])
    c10 = get_cat(pm10, [45, 50, 132, 213])
    max_cat = max(c25, c10)
    return max_cat if max_cat > 0 else None

def obtener_info_ica(valor_cat):
    VERDE = "#9ACA3C"
    AMARILLO = "#F7EC0F"
    NARANJA = "#F8991D"
    ROJO = "#ED2124"
    MORADO = "#7D287D"
    GRIS = "gray"

    if pd.isna(valor_cat) or valor_cat == 0:
        return "SIN DATOS", GRIS
    try:
        cat = int(valor_cat)
        colores = {1:("BUENA", VERDE), 2:("ACEPTABLE", AMARILLO), 
                   3:("MALA", NARANJA), 4:("MUY MALA", ROJO), 
                   5:("EXTREMADAMENTE MALA", MORADO)}
        return colores.get(cat, ("SIN DATOS", GRIS))
    except:
        return "SIN DATOS", GRIS


def descargar_sinaica():
    # Sinaica no ofrece endpoint limpio, entonces se extrae el state desde DOM
    url = "https://sinaica.inecc.gob.mx/index.php"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers, verify=False, timeout=20)
        match = re.search(r"let cump = ({.*?});", response.text, re.S)
        if not match: return None

        data_json = json.loads(match.group(1))
        estaciones_lista = []

        def limpiar(v):
            if v in [None, 'DI', 'null', 'ND', '', 'SR', '--']: return None
            try: return float(str(v).strip().replace(',', ''))
            except: return None

        for _, edo in data_json.items():
            if isinstance(edo, dict) and 'redes' in edo:
                for _, red in edo['redes'].items():
                    if isinstance(red, dict) and 'ests' in red:
                        for _, est in red['ests'].items():
                            if isinstance(est, dict):
                                calc = est.get('calc')
                                if isinstance(calc, dict) and ('PM2.5' in calc or 'PM10' in calc):
                                    p25 = limpiar(calc.get('PM2.5'))
                                    p10 = limpiar(calc.get('PM10'))
                                    lat = limpiar(est.get('lat'))
                                    lon = limpiar(est.get('long'))
                                    
                                    if lat and lon:
                                        estaciones_lista.append({
                                            'estacion': est.get('nom'),
                                            'lat': lat, 'lon': lon,
                                            'pm25': p25, 'pm10': p10,
                                            'estado': edo.get('nom'),
                                            'ica_valor': calcular_categoria_estacion(p25, p10)
                                        })
        return pd.DataFrame(estaciones_lista)
    except Exception as e:
        print(f"Error descarga SINAICA: {e}")
        return None

def descargar_poligono_mexico():
    urls = [
        "https://raw.githubusercontent.com/johan/world.geo.json/master/countries/MEX.geo.json",
        "https://raw.githubusercontent.com/angelnmara/geojson/master/mexico/mexico.json"
    ]
    headers = {'User-Agent': 'Mozilla/5.0'}
    for url in urls:
        try:
            r = requests.get(url, headers=headers, verify=False, timeout=20)
            if r.status_code == 200: return r.json()
        except: pass
    return None


def generar_capa_nacional(df):
    global CACHE_MASK
    
    if df.empty: return None, None
    
    GRID_RES = 0.02 
    LAT_MIN, LAT_MAX = 14.5, 33.0
    LON_MIN, LON_MAX = -118.0, -86.5
    
    grid_lat = np.arange(LAT_MIN, LAT_MAX, GRID_RES)
    grid_lon = np.arange(LON_MIN, LON_MAX, GRID_RES)
    
    mesh_lon, mesh_lat = np.meshgrid(grid_lon, grid_lat)
    shape_grid = mesh_lat.shape
    grid_coords = np.vstack([mesh_lat.ravel(), mesh_lon.ravel()]).T
    
    # Mascarilla para evitar procesar puntos externos
    if CACHE_MASK is not None and CACHE_MASK.shape == shape_grid:
        mascara_mexico = CACHE_MASK
    else:
        geo = descargar_poligono_mexico()
        if not geo: return None, None

        mascara_mexico = np.zeros(grid_coords.shape[0], dtype=bool)
        puntos_para_mascara = grid_coords[:, [1, 0]] 
        
        try:
            features = geo.get('features', [geo]) 
            for feature in features:
                geom = feature['geometry']
                polys = []
                if geom['type'] == 'Polygon': polys.append(geom['coordinates'][0])
                elif geom['type'] == 'MultiPolygon': 
                    for p in geom['coordinates']: polys.append(p[0])
                
                for coords in polys:
                    path = MplPath(coords)
                    p_arr = np.array(coords)
                    p_min_x, p_min_y = p_arr[:,0].min(), p_arr[:,1].min()
                    p_max_x, p_max_y = p_arr[:,0].max(), p_arr[:,1].max()
                    
                    candidatos = (
                        (puntos_para_mascara[:,0] >= p_min_x) & (puntos_para_mascara[:,0] <= p_max_x) &
                        (puntos_para_mascara[:,1] >= p_min_y) & (puntos_para_mascara[:,1] <= p_max_y)
                    )
                    if np.any(candidatos):
                        mascara_mexico[candidatos] |= path.contains_points(puntos_para_mascara[candidatos])
        except: return None, None

        CACHE_MASK = mascara_mexico.reshape(shape_grid)
        mascara_mexico = CACHE_MASK

    if not np.any(mascara_mexico): return None, None

    # Interpolacion sobre valores
    def calc_idw(df_sub, col_name):
        if df_sub.empty:
            return np.full(shape_grid, np.nan)
            
        est_coords = df_sub[['lat', 'lon']].values
        valores_crudos = df_sub[col_name].values
        
        dists = cdist(grid_coords, est_coords)
        dists[dists == 0] = 1e-9 # Evitar dividir sobre 0 en coincidencia
        
        weights = 1.0 / np.power(dists, 4)
        num = np.sum(weights * valores_crudos, axis=1)
        den = np.sum(weights, axis=1)
        
        z_grid = (num / den).reshape(shape_grid)
        
        for _, fila in df_sub.iterrows():
            lat_est, lon_est = fila['lat'], fila['lon']
            val_est = fila[col_name]
            idx_lat = int(round((lat_est - LAT_MIN) / GRID_RES))
            idx_lon = int(round((lon_est - LON_MIN) / GRID_RES))
            if 0 <= idx_lat < shape_grid[0] and 0 <= idx_lon < shape_grid[1]:
                z_grid[idx_lat, idx_lon] = val_est
        return z_grid

    df_pm10 = df.dropna(subset=['pm10'])
    df_pm25 = df.dropna(subset=['pm25'])

    grid_pm10 = calc_idw(df_pm10, 'pm10')
    grid_pm25 = calc_idw(df_pm25, 'pm25')

    def categorize_grid(v_grid, limites):
        if np.all(np.isnan(v_grid)):
            return np.zeros_like(v_grid)
        val = np.floor(v_grid + 0.5) 
        cat = np.ones_like(val)
        cat = np.where(val > limites[0], 2, cat)
        cat = np.where(val > limites[1], 3, cat)
        cat = np.where(val > limites[2], 4, cat)
        cat = np.where(val > limites[3], 5, cat)
        cat[np.isnan(v_grid)] = 0
        return cat

    cat10 = categorize_grid(grid_pm10, [45, 50, 132, 213])
    cat25 = categorize_grid(grid_pm25, [15, 25, 79, 130])

    z_final = np.maximum(cat10, cat25)

    z_final = np.where(mascara_mexico, z_final, np.nan)
    z_final = np.where(z_final == 0, np.nan, z_final)
    
    colores_hex = ['#9ACA3C', '#F7EC0F', '#F8991D', '#ED2124', '#7D287D']
    cmap_custom = colors.ListedColormap(colores_hex)
    bounds = [0.5, 1.5, 2.5, 3.5, 4.5, 5.5]
    norm = colors.BoundaryNorm(bounds, cmap_custom.N)
    
    dpi = 96
    fig = plt.figure(figsize=(z_final.shape[1]/dpi, z_final.shape[0]/dpi), dpi=dpi)
    ax = plt.Axes(fig, [0., 0., 1., 1.])
    ax.set_axis_off()
    fig.add_axes(ax)
    
    # interpolation='none' respeta la naturaleza discreta de la categorización
    ax.imshow(z_final, cmap=cmap_custom, norm=norm, origin='lower', 
              interpolation='none', aspect='auto')
    
    img_buffer = BytesIO()
    plt.savefig(img_buffer, format='png', transparent=True, bbox_inches='tight', pad_inches=0)
    plt.close(fig)
    
    img_b64 = base64.b64encode(img_buffer.getvalue()).decode('utf-8')
    half_pixel = GRID_RES / 2.0
    
    LAT_OFFSET = -0.26
    image_bounds = [
        [grid_lat[0]-half_pixel + LAT_OFFSET, grid_lon[0]-half_pixel],
        [grid_lat[-1]+half_pixel + LAT_OFFSET, grid_lon[-1]+half_pixel]
    ]
    return f'data:image/png;base64,{img_b64}', image_bounds


@app.route('/')
def index():
    global CACHE_HTML, LAST_UPDATE
    
    current_time = time.time()
    
    if (current_time - LAST_UPDATE > TIEMPO_REFRESCO) or not HISTORY_BUFFER:
        try:
            df = descargar_sinaica()
            
            if df is not None and not df.empty:
                img_url, bounds = generar_capa_nacional(df)
                if img_url:
                    timestamp_str = datetime.now().strftime("%d/%m %H:%M")
                    
                    registro = {
                        "time": timestamp_str,
                        "img": img_url,
                        "bounds": bounds,
                        "df": df
                    }
                    HISTORY_BUFFER.append(registro)
                    LAST_UPDATE = current_time
        except Exception:
            pass

    try:
        mapa = folium.Map(location=[23.63, -102.55], zoom_start=5, tiles="CartoDB positron")
        
        total_frames = len(HISTORY_BUFFER)
        fechas_js = []
        
        for i, registro in enumerate(HISTORY_BUFFER):
            es_ultimo = (i == total_frames - 1)
            opacidad_inicial = 0.65 if es_ultimo else 0.0
            
            # Etiquetado por className para toggle vía slider
            folium.raster_layers.ImageOverlay(
                registro["img"],
                registro["bounds"],
                opacity=opacidad_inicial,
                name=f"IgnoradoPorJs_{i}", 
                interactive=False,
                zindex=1,
                className=f"layer_img_{i}" 
            ).add_to(mapa)
            
            for _, r in registro["df"].iterrows():
                ica = r.get('ica_valor')
                txt, col = obtener_info_ica(ica)
                p10 = r['pm10'] if pd.notna(r['pm10']) else "--"
                p25 = r['pm25'] if pd.notna(r['pm25']) else "--"
                
                pop = f"""
                <div style="font-family:Arial;width:200px">
                <b>{r['estacion']}</b><br>{r['estado']}<hr>
                PM10: {p10} µg/m³<br>PM2.5: {p25} µg/m³<br>
                <div style="background:{col};text-align:center"><b>{txt}</b></div>
                </div>"""
                
                op_marker = 1.0 if es_ultimo else 0.0
                fill_op_marker = 1.0 if es_ultimo else 0.0
                
                folium.CircleMarker(
                    [r['lat'], r['lon']], 
                    radius=5, 
                    color='black', 
                    weight=1,
                    fill_color=col, 
                    fill_opacity=fill_op_marker, 
                    opacity=op_marker,
                    popup=folium.Popup(pop, max_width=220),
                    className=f"layer_marker_{i}" 
                ).add_to(mapa)
            
            fechas_js.append(registro["time"])

        fecha_actual = fechas_js[-1] if fechas_js else "Cargando..."
        leyenda_html = f'''
             <div style="position: fixed; bottom: 30px; left: 30px; width: 220px; z-index:9999; 
             background-color: white; border:2px solid grey; padding: 12px; opacity: 0.95; font-family: sans-serif; border-radius: 5px; box-shadow: 0 0 5px rgba(0,0,0,0.3);">
                 <b style="font-size: 14px; color: #333;">Calidad del Aire (ICA)</b><br>
                 <span style="font-size: 11px; color: #666;">Actualizado: {fecha_actual}</span>
                 <hr style="margin: 8px 0; border: 0; border-top: 1px solid #ccc;">
                 <div style="font-size: 12px; line-height: 1.8; color: #333;">
                     <i style="background: #9ACA3C; width: 14px; height: 14px; float: left; margin-right: 8px; margin-top: 2px; border-radius: 3px; border: 1px solid #777;"></i> Buena<br>
                     <i style="background: #F7EC0F; width: 14px; height: 14px; float: left; margin-right: 8px; margin-top: 2px; border-radius: 3px; border: 1px solid #777;"></i> Aceptable<br>
                     <i style="background: #F8991D; width: 14px; height: 14px; float: left; margin-right: 8px; margin-top: 2px; border-radius: 3px; border: 1px solid #777;"></i> Mala<br>
                     <i style="background: #ED2124; width: 14px; height: 14px; float: left; margin-right: 8px; margin-top: 2px; border-radius: 3px; border: 1px solid #777;"></i> Muy Mala<br>
                     <i style="background: #7D287D; width: 14px; height: 14px; float: left; margin-right: 8px; margin-top: 2px; border-radius: 3px; border: 1px solid #777;"></i> Ext. Mala
                 </div>
             </div>
        '''
        mapa.get_root().html.add_child(folium.Element(leyenda_html))

        if fechas_js:
            start_hh = fechas_js[0].split(' ')[1].split(':')[0] + "h"
            end_hh = fechas_js[-1].split(' ')[1].split(':')[0] + "h"
        else:
            start_hh = "--"; end_hh = "--"

        slider_html = f"""
        <style>
            .slider-container {{
                position: fixed; bottom: 30px; right: 30px; 
                width: 50%; max-width: 800px; z-index: 9999; 
                background: white; padding: 15px; border-radius: 5px; 
                border: 2px solid grey; box-shadow: 0 0 5px rgba(0,0,0,0.3);
            }}
            input[type=range] {{ width: 100%; margin: 5px 0; }}
            .ticks {{ display: flex; justify-content: space-between; padding: 0 10px; margin-top: -5px; }}
            .tick {{ width: 1px; background: #ccc; height: 5px; }}
        </style>

        <div class="slider-container">
            <div style="text-align: center; margin-bottom:5px; font-weight:bold; color:#444;">Historial (Últimas 12h)</div>
            <input type="range" min="0" max="{max(1, total_frames - 1)}" value="{max(0, total_frames - 1)}" step="1"
                   class="slider" id="timeSlider" style="cursor: pointer;">
            <div class="ticks">{''.join(['<span class="tick"></span>']*12)}</div>
            <div style="display:flex; justify-content:space-between; font-size:14px; font-weight:bold; font-family:sans-serif; color:black; margin-top:5px;">
                <span>{start_hh}</span><span>{end_hh}</span>
            </div>
            <div style="text-align: center; font-size: 11px; color: gray;">{total_frames} registro(s)</div>
        </div>

        <script>
            var slider = document.getElementById("timeSlider");
            
            function updateMap(idx) {{
                var map_div = document.querySelector('.folium-map');
                var map_id = map_div ? map_div.id : null;
                var map_obj = map_id ? window[map_id] : null;

                if (!map_obj) return;

                map_obj.eachLayer(function (layer) {{
                    if (layer.options && layer.options.className) {{
                        var cName = layer.options.className;
                        
                        if (cName.includes('layer_img_')) {{
                            var layer_idx = parseInt(cName.split('_')[2]);
                            if (layer_idx === idx) layer.setOpacity(0.65);
                            else layer.setOpacity(0);
                        }}
                        
                        if (cName.includes('layer_marker_')) {{
                            var group_idx = parseInt(cName.split('_')[2]);
                            
                            // Leaflet gestiona opacidad via setStyle
                            if (group_idx === idx) {{
                                layer.setStyle({{opacity: 1, fillOpacity: 1}});
                                if (layer.getElement()) layer.getElement().style.pointerEvents = 'auto';
                            }} else {{
                                layer.setStyle({{opacity: 0, fillOpacity: 0}});
                                if (layer.getElement()) layer.getElement().style.pointerEvents = 'none';
                            }}
                        }}
                    }}
                }});
            }}
            
            slider.addEventListener('input', function() {{
                var max_idx = {total_frames - 1};
                var val = parseInt(this.value);
                if (val > max_idx) val = max_idx;
                updateMap(val);
            }});
        </script>
        """
        mapa.get_root().html.add_child(folium.Element(slider_html))
        
        mapa.get_root().header.add_child(folium.Element(f'<meta http-equiv="refresh" content="{TIEMPO_REFRESCO}">'))
        CACHE_HTML = mapa.get_root().render()
        return CACHE_HTML

    except Exception:
        return f"<pre>{traceback.format_exc()}</pre>"

if __name__ == '__main__':
    threading.Timer(2, lambda: webbrowser.open_new(f"http://127.0.0.1:{PUERTO}/")).start()
    app.run(port=PUERTO, debug=False, use_reloader=False)
