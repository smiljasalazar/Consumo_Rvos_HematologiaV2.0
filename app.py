import streamlit as st
import pandas as pd
import re
import io
from datetime import timedelta

st.set_page_config(page_title="Consumo de Reactivos Mindray", layout="wide")

# =========================================================================
# 1. REACTIVOS: nombres canónicos + matriz por MODO BASE
# =========================================================================
DS, LH, LD, FD, LN, FN, DR, FR, VSG = (
    "DS_DILUENTE", "M6_LH_LYSE", "M6_LD_LYSE", "M6_FD_DYE",
    "M6_LN_LYSE", "M6_FN_DYE", "M6_DR_DILUENT", "M6_FR_DYE", "VSG",
)
# Reactivos vistos en algunos equipos que aún no están confirmados en la matriz
# (se registran para no perder el dato, pero sin modos asignados hasta confirmar)
LM, FM = "M6_LM_LYSE_SINCONFIRMAR", "M6_FM_DYE_SINCONFIRMAR"

REAGENT_DISPLAY = {
    DS: "DS DILUENTE", LH: "M-6 LH LYSE", LD: "M-6 LD LYSE", FD: "M-6 FD DYE",
    LN: "M-6 LN LYSE", FN: "M-6 FN DYE", DR: "M-6 DR DILUENT", FR: "M-6 FR DYE",
    VSG: "Reactivo solución VSG",
    LM: "M-6 LM LYSE", FM: "M-6 FM DYE",
}

def normalize_reagent_name(raw_name):
    """Reconoce el reactivo sin importar la variante de nombre usada por cada modelo."""
    if not isinstance(raw_name, str):
        return None
    name = raw_name.upper()
    if "VSG" in name:
        return VSG
    if "DS" in name and "DILUY" in name:
        return DS
    if "DR" in name:
        return DR
    if "FR" in name:
        return FR
    if "FM" in name:
        return FM
    if "FD" in name:
        return FD
    if "FN" in name:
        return FN
    if "LH" in name:
        return LH
    if "LM" in name:
        return LM
    if "LD" in name:
        return LD
    if "LN" in name:
        return LN
    return None

# Reactivo -> MODOS BASE que lo consumen (no strings de panel compuesto tipo "CD+VSG")
REAGENT_MODES = {
    DS:  {"CD", "CBC", "CDR", "RET", "CR", "PLT-O"},
    LH:  {"CBC", "CD", "CDR", "CR"},
    LD:  {"CD", "CDR"},
    FD:  {"CD", "CDR"},
    LN:  {"CDR", "CD"},
    FN:  {"CDR", "CD"},
    DR:  {"CDR", "RET", "CR", "PLT-O"},
    FR:  {"CDR", "RET", "CR", "PLT-O"},
    VSG: {"ESR"},
    LM:  {"HMC"},
    FM:  {"HMC"},
}

def reagent_used_by_control(reagent, lot):
    """MB -> todos menos DR/FR.  ME -> DR/FR y también DS DILUENTE.  Otro prefijo -> None (revisar)."""
    lot = (lot or "").upper()
    if lot.startswith("MB"):
        return reagent not in (DR, FR)
    if lot.startswith("ME"):
        return reagent in (DR, FR, DS)
    return None


# Sinónimos de componentes de panel -> modo base canónico (paneles compuestos como CD+VSG, CDR/PLT-8X)
PANEL_SYNONYMS = {
    "CD": "CD", "CDR": "CDR", "CBC": "CBC", "RET": "RET", "CR": "CR",
    "VSG": "ESR", "ESR": "ESR",
    "PLT-O": "PLT-O", "PLT-8X": "PLT-O",
    "WBC-3X": "CD",
    "HMC": "HMC",
}

def decompose_panel(panel_str):
    """'CD+VSG' -> ({'CD','ESR'}, set()).  Componentes no reconocidos (p.ej. 'HMC') se separan."""
    if not isinstance(panel_str, str):
        return set(), set()
    parts = re.split(r"[+/]", panel_str.strip().upper())
    canon, unknown = set(), set()
    for p in parts:
        p = p.strip()
        if not p:
            continue
        mapped = PANEL_SYNONYMS.get(p)
        (canon.add(mapped) if mapped else unknown.add(p))
    return canon, unknown


# =========================================================================
# 2. LECTURA Y DETECCIÓN FLEXIBLE DE COLUMNAS (varía según modelo)
# =========================================================================

def read_mindray_csv(file_obj):
    file_obj.seek(0)
    return pd.read_csv(file_obj, encoding="utf-16-le", sep="\t")

def pick_col(df, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None

SERIAL_RE = re.compile(r"^(?:Log|Review|LJQC)_([A-Za-z0-9\-]+)_\d{4}")

def get_serial(filename):
    m = SERIAL_RE.match(filename)
    return m.group(1) if m else None


DETAIL_RE = re.compile(r"^\s*([^:]+?):.*?Volum\.\:\s*([\d.]+)\s*->\s*([\d.]+)", re.IGNORECASE)

def parse_log_reactivos(file_obj, dedup_minutes=60):
    df = read_mindray_csv(file_obj)
    resumen_col = pick_col(df, ["Resumen"])
    fecha_col = pick_col(df, ["Fech/Hora", "Fecha/Hora"])
    detalle_col = pick_col(df, ["Detalle"])
    if not (resumen_col and fecha_col and detalle_col):
        raise ValueError("El archivo de Log no tiene las columnas esperadas (Resumen / Fech/Hora / Detalle).")

    df = df[df[resumen_col].astype(str).str.strip() == "Modif conf reactivos"].copy()
    df["_fecha_hora"] = pd.to_datetime(df[fecha_col], dayfirst=True, errors="coerce")

    parsed = df[detalle_col].astype(str).apply(lambda d: DETAIL_RE.match(d.strip()))
    df["reactivo_raw"] = parsed.apply(lambda m: m.group(1).strip() if m else None)
    df["reactivo"] = df["reactivo_raw"].apply(normalize_reagent_name)

    no_reconocidos = sorted(df.loc[df["reactivo_raw"].notna() & df["reactivo"].isna(), "reactivo_raw"].unique().tolist())

    df = df.dropna(subset=["reactivo", "_fecha_hora"]).copy()
    df = df.sort_values(["reactivo", "_fecha_hora"]).reset_index(drop=True)

    keep_rows, last_kept_time = [], {}
    for _, row in df.iterrows():
        r, t = row["reactivo"], row["_fecha_hora"]
        if r in last_kept_time and (t - last_kept_time[r]) <= timedelta(minutes=dedup_minutes):
            continue
        keep_rows.append(row)
        last_kept_time[r] = t

    events = pd.DataFrame(keep_rows).reset_index(drop=True)
    if events.empty:
        events = pd.DataFrame(columns=["reactivo", "fecha_hora"])
    else:
        events = events[["reactivo", "_fecha_hora"]].rename(columns={"_fecha_hora": "fecha_hora"})
    events = events.sort_values(["reactivo", "fecha_hora"]).reset_index(drop=True)
    events.attrs["reactivos_no_reconocidos"] = no_reconocidos
    return events


def parse_review(file_obj):
    df = read_mindray_csv(file_obj)
    panel_col = pick_col(df, ["Panel prue", "Panel pr"])
    fecha_col = pick_col(df, ["Fec.", "Fech"])
    hora_col = pick_col(df, ["Hora"])
    if not (panel_col and fecha_col and hora_col):
        raise ValueError(
            "Este Review no tiene el formato esperado de listado de muestras (faltan columnas "
            "de Panel/Fecha/Hora). Puede ser otro tipo de reporte (p.ej. productividad) — "
            "sube el CSV de resultados de muestras individuales."
        )
    out = pd.DataFrame()
    out["panel_raw"] = df[panel_col].astype(str).str.strip().str.upper()
    out["fecha_hora"] = pd.to_datetime(
        df[fecha_col].astype(str).str.strip() + " " + df[hora_col].astype(str).str.strip(),
        dayfirst=True, errors="coerce",
    )
    out = out.dropna(subset=["fecha_hora"])
    decompuesto = out["panel_raw"].apply(decompose_panel)
    out["modos_canon"] = decompuesto.apply(lambda t: t[0])
    componentes_no_reconocidos = sorted(set().union(*decompuesto.apply(lambda t: t[1]))) if len(out) else []
    out.attrs["componentes_panel_no_reconocidos"] = componentes_no_reconocidos
    return out.reset_index(drop=True)


def parse_ljqc(file_obj):
    file_obj.seek(0)
    raw_lines = io.TextIOWrapper(file_obj, encoding="utf-16-le").readlines()

    def cell(line, idx):
        parts = line.rstrip("\n").split("\t")
        return parts[idx].strip() if idx < len(parts) else ""

    lote = cell(raw_lines[0], 4)
    nivel = cell(raw_lines[0], 7)
    modo = cell(raw_lines[1], 4)
    panel = cell(raw_lines[2], 1).strip().upper()

    rows = []
    for line in raw_lines[4:]:
        parts = line.rstrip("\n").split("\t")
        if not parts or parts[0].strip() in ("Destin", "Límit", ""):
            continue
        fecha = parts[1].strip() if len(parts) > 1 else ""
        hora = parts[2].strip() if len(parts) > 2 else ""
        if not fecha or not hora:
            continue
        rows.append({"fecha": fecha, "hora": hora})

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["fecha_hora"] = pd.to_datetime(out["fecha"] + " " + out["hora"], dayfirst=True, errors="coerce")
    out = out.dropna(subset=["fecha_hora"])
    out["lote"] = lote
    out["nivel"] = nivel
    out["modo"] = modo
    out["panel_raw"] = panel if panel else "CD"
    decompuesto = out["panel_raw"].apply(decompose_panel)
    out["modos_canon"] = decompuesto.apply(lambda t: t[0])
    return out[["fecha_hora", "lote", "nivel", "modo", "panel_raw", "modos_canon"]].reset_index(drop=True)


def parse_all_controls(file_objs):
    frames = [parse_ljqc(f) for f in file_objs]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=["fecha_hora", "lote", "nivel", "modo", "panel_raw", "modos_canon"])
    return pd.concat(frames, ignore_index=True)


# =========================================================================
# 3. RENDIMIENTO (SOLO CANTIDAD DE PRUEBAS, SIN VOLUMEN)
# =========================================================================

def compute_consumption(events, samples, controls):
    detail_rows = []
    for reactivo, ev in events.groupby("reactivo"):
        ev = ev.sort_values("fecha_hora").reset_index(drop=True)
        modes_for_reagent = REAGENT_MODES.get(reactivo, set())

        for i in range(len(ev)):
            start = ev.loc[i, "fecha_hora"]
            is_last = i == len(ev) - 1
            end = ev.loc[i + 1, "fecha_hora"] if not is_last else None

            mask_s = (samples["fecha_hora"] >= start) & (samples["fecha_hora"] < end) if end is not None else samples["fecha_hora"] >= start
            sub_s = samples.loc[mask_s]
            n_samples = int(sub_s["modos_canon"].apply(lambda s: bool(s & modes_for_reagent)).sum()) if len(sub_s) else 0

            mask_c = (controls["fecha_hora"] >= start) & (controls["fecha_hora"] < end) if end is not None else controls["fecha_hora"] >= start
            sub_c = controls.loc[mask_c].copy()
            if not sub_c.empty:
                sub_c["usa_reactivo"] = sub_c["lote"].apply(lambda lot: reagent_used_by_control(reactivo, lot))
                n_controls = int(sub_c["usa_reactivo"].fillna(False).sum())
                n_no_reconocido = int(sub_c["usa_reactivo"].isna().sum())
            else:
                n_controls, n_no_reconocido = 0, 0

            detail_rows.append({
                "reactivo": REAGENT_DISPLAY.get(reactivo, reactivo),
                "inicio_periodo": start,
                "fin_periodo": end if end is not None else "EN USO (frasco actual)",
                "n_muestras": n_samples,
                "n_controles": n_controls,
                "n_controles_lote_no_reconocido": n_no_reconocido,
                "n_pruebas_total": n_samples + n_controls,
            })
    return pd.DataFrame(detail_rows)


def summarize_by_reagent(detail):
    cerradas = detail[detail["fin_periodo"] != "EN USO (frasco actual)"].copy()
    if cerradas.empty:
        return pd.DataFrame(columns=["reactivo", "cajas_completas", "promedio_pruebas_x_caja", "minimo", "maximo"])
    summary = cerradas.groupby("reactivo")["n_pruebas_total"].agg(
        cajas_completas="count", promedio_pruebas_x_caja="mean", minimo="min", maximo="max",
    ).round(1).reset_index()
    return summary.sort_values("reactivo").reset_index(drop=True)


def to_excel_bytes(summary, detail, samples, controls, events):
    buf = io.BytesIO()
    muestras_por_panel = samples["panel_raw"].value_counts().rename_axis("Panel").reset_index(name="n_muestras")
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="Rendimiento_x_Caja", index=False)
        muestras_por_panel.to_excel(xw, sheet_name="Muestras_por_Panel", index=False)
        controls.drop(columns=["modos_canon"], errors="ignore").to_excel(xw, sheet_name="Controles", index=False)
        events.rename(columns={"fecha_hora": "fecha_cambio"}).to_excel(xw, sheet_name="Fechas_de_Cambio", index=False)
        for reactivo in detail["reactivo"].unique():
            sub = detail[detail["reactivo"] == reactivo].copy().reset_index(drop=True)
            sub.insert(0, "Nº caja/cambio", range(1, len(sub) + 1))
            sub = sub.rename(columns={
                "inicio_periodo": "Fecha inicio caja", "fin_periodo": "Fecha fin caja (o en uso)",
                "n_muestras": "Muestras", "n_controles": "Controles", "n_pruebas_total": "TOTAL pruebas",
            })[["Nº caja/cambio", "Fecha inicio caja", "Fecha fin caja (o en uso)", "Muestras", "Controles", "TOTAL pruebas"]]
            hoja = reactivo.replace(" ", "_")[:31]
            sub.to_excel(xw, sheet_name=hoja, index=False)
    buf.seek(0)
    return buf


# =========================================================================
# 4. INTERFAZ STREAMLIT
# =========================================================================

st.title("🧪 Consumo de Reactivos — Analizadores Hematológicos Mindray")
st.caption("BC-6000 · BC-6200 · BC-6800 PLUS · BC-760 · BC-780")

with st.expander("ℹ️ Cómo funciona", expanded=False):
    st.markdown("""
    1. Sube el **Log** de cambios de reactivo, el **Review** de resultados, y opcionalmente
       hasta 6 archivos **LJQC** de control (bajo/medio/alto). Funciona con los distintos
       modelos Mindray aunque tengan nombres de columna ligeramente distintos.
    2. Cada cambio de reactivo en el Log define el inicio de una caja/frasco nuevo. Entre dos
       cambios consecutivos se cuenta cuántas muestras/controles se hicieron con esa caja.
    3. Reglas de control: lote **MB** = todos los reactivos excepto DR DILUENT/FR DYE;
       lote **ME** = DR DILUENT/FR DYE y también DS DILUENTE.
    4. Paneles compuestos (`CD+VSG`, `CDR/PLT-8X`, etc.) se separan en sus modos base
       automáticamente.
    5. Eventos de cambio duplicados a menos de 60 minutos se deduplican, conservando el primero.
    """)

col1, col2 = st.columns(2)
with col1:
    log_file = st.file_uploader("Log de cambio de reactivos (Log_....csv)", type="csv")
with col2:
    review_file = st.file_uploader("Resultados de muestras (Review_....csv)", type="csv")

incluir_controles = st.checkbox("Incluir archivos de control LJQC (opcional)", value=False)
ljqc_files = []
if incluir_controles:
    st.markdown("**Archivos de control LJQC (hasta 6: bajo/medio/alto)**")
    ljqc_cols = st.columns(3)
    for i in range(6):
        with ljqc_cols[i % 3]:
            f = st.file_uploader(f"Control {i + 1}", type="csv", key=f"ljqc_{i}", label_visibility="collapsed")
            if f is not None:
                ljqc_files.append(f)

dedup_minutes = st.sidebar.slider("Umbral de deduplicación de cambios (minutos)", 5, 180, 60, 5)

if log_file and review_file:
    serial_log = get_serial(log_file.name)
    serial_review = get_serial(review_file.name)
    if serial_log and serial_review and serial_log != serial_review:
        st.warning(
            f"⚠️ El Log parece ser del equipo **{serial_log}** y el Review del equipo "
            f"**{serial_review}** — nombres de serie distintos en el archivo. Si no son el mismo "
            f"analizador, los resultados no van a tener sentido."
        )

    try:
        with st.spinner("Procesando..."):
            events = parse_log_reactivos(log_file, dedup_minutes=dedup_minutes)
            samples = parse_review(review_file)
            controls = parse_all_controls(ljqc_files) if ljqc_files else pd.DataFrame(
                columns=["fecha_hora", "lote", "nivel", "modo", "panel_raw", "modos_canon"]
            )
            detail = compute_consumption(events, samples, controls)
            summary = summarize_by_reagent(detail)
    except ValueError as e:
        st.error(str(e))
        st.stop()

    m1, m2, m3 = st.columns(3)
    m1.metric("Eventos de cambio (deduplicados)", len(events))
    m2.metric("Muestras cargadas", len(samples))
    m3.metric("Corridas de control cargadas", len(controls))

    no_reconocidos = events.attrs.get("reactivos_no_reconocidos", [])
    if no_reconocidos:
        st.warning(f"Nombres de reactivo en el Log que no reconocí: {no_reconocidos}. No se contaron.")

    comp_no_reconocidos = samples.attrs.get("componentes_panel_no_reconocidos", [])
    if comp_no_reconocidos:
        st.info(
            f"Componentes de panel no reconocidos en las muestras (se ignoran, no afectan el resto "
            f"del panel): {comp_no_reconocidos}"
        )

    lotes_control = controls["lote"].dropna().unique() if not controls.empty else []
    lotes_no_reconocidos = [l for l in lotes_control if not (str(l).upper().startswith("MB") or str(l).upper().startswith("ME"))]
    if len(lotes_no_reconocidos):
        st.warning(f"Lotes de control con prefijo no reconocido (ni MB ni ME): {lotes_no_reconocidos}")

    if not events.empty:
        n_muestras_antes = samples[samples["fecha_hora"] < events["fecha_hora"].min()].shape[0]
        if n_muestras_antes:
            st.info(
                f"{n_muestras_antes} muestras son anteriores al cambio de reactivo más antiguo "
                f"registrado en el Log y no entran en ningún periodo de consumo."
            )
        n_solapadas = samples[
            (samples["fecha_hora"] >= events["fecha_hora"].min()) & (samples["fecha_hora"] <= events["fecha_hora"].max())
        ].shape[0]
        if n_solapadas == 0:
            st.error(
                "Ninguna muestra del Review cae dentro del rango de fechas del Log — probablemente "
                "son de periodos distintos. Sube archivos que cubran las mismas fechas."
            )

    st.subheader("📦 Rendimiento por caja/botella (cantidad de pruebas)")
    st.caption("Calculado solo sobre cajas ya terminadas (se excluye la que sigue en uso).")
    st.dataframe(summary, use_container_width=True)
    if not summary.empty:
        st.bar_chart(summary.set_index("reactivo")["promedio_pruebas_x_caja"])

    abiertas = detail[detail["fin_periodo"] == "EN USO (frasco actual)"][["reactivo", "n_pruebas_total"]]
    abiertas = abiertas.rename(columns={"n_pruebas_total": "pruebas_caja_actual"})
    comparacion = abiertas.merge(summary[["reactivo", "promedio_pruebas_x_caja"]], on="reactivo", how="left")
    if not comparacion.empty:
        comparacion["%_del_promedio"] = (
            comparacion["pruebas_caja_actual"] / comparacion["promedio_pruebas_x_caja"] * 100
        ).round(0)
        st.markdown("**Caja actual en uso vs. promedio histórico:**")
        st.dataframe(comparacion, use_container_width=True)

    with st.expander("Cantidad de pruebas por caja/cambio (por reactivo)"):
        reactivo_sel = st.selectbox("Reactivo", sorted(detail["reactivo"].unique()))
        sub = detail[detail["reactivo"] == reactivo_sel].copy().reset_index(drop=True)
        sub.insert(0, "Nº caja/cambio", range(1, len(sub) + 1))
        sub = sub.rename(columns={
            "inicio_periodo": "Fecha inicio caja", "fin_periodo": "Fecha fin caja (o en uso)",
            "n_muestras": "Muestras", "n_controles": "Controles", "n_pruebas_total": "TOTAL pruebas",
        })[["Nº caja/cambio", "Fecha inicio caja", "Fecha fin caja (o en uso)", "Muestras", "Controles", "TOTAL pruebas"]]
        st.dataframe(sub, use_container_width=True)

    excel_bytes = to_excel_bytes(summary, detail, samples, controls, events)
    st.download_button(
        "📥 Descargar reporte Excel completo",
        data=excel_bytes,
        file_name="Consumo_Reactivos.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
else:
    st.info("Sube al menos el archivo Log y el archivo Review para comenzar.")
