from googleapiclient.discovery import build
from moduli.google_auth import get_credentials
from datetime import datetime, timezone, date


_METRICHE = ("views,averageViewDuration,averageViewPercentage,"
             "estimatedMinutesWatched,impressions,impressionClickThroughRate")


def _api_disabilitata(errore: Exception) -> bool:
    """L'API Analytics non è abilitata sul progetto Google.

    Non è un errore per singolo video: riprovare una query alla volta produce
    lo stesso 403 per ognuno, cioè cinque muri di testo identici a ogni run.
    """
    testo = str(errore)
    return ("accessNotConfigured" in testo
            or "SERVICE_DISABLED" in testo
            or "has not been used in project" in testo)


def _analytics_per_video(yta, video_ids: list[str]) -> tuple[dict[str, tuple], bool]:
    """Metriche di tutti i video in UNA sola query, con `dimensions=video`.

    Prima si faceva una query YouTube Analytics per ogni video: 10 chiamate a
    ogni run della pipeline, per dati identici. Se la query aggregata fallisce
    si ricade sulle query singole, così il comportamento resta invariato sui
    canali dove la dimensione `video` non è disponibile.

    Ritorna `(metriche, disponibile)`: `disponibile=False` significa "non lo
    sappiamo", non "tutto a zero" — chi legge deve poter distinguere i due casi.
    """
    if not video_ids:
        return {}, True
    oggi = date.today().isoformat()
    try:
        resp = (
            yta.reports()
            .query(
                ids="channel==MINE",
                startDate="2020-01-01",
                endDate=oggi,
                metrics=_METRICHE,
                dimensions="video",
                filters="video==" + ",".join(video_ids),
                maxResults=len(video_ids),
            )
            .execute()
        )
        # la prima colonna e' la dimensione `video`, le altre sono le metriche
        return {r[0]: tuple(r[1:7]) for r in resp.get("rows", []) if r}, True
    except Exception as e:
        if _api_disabilitata(e):
            print("[analytics] YouTube Analytics API non abilitata sul progetto Google: "
                  "CTR, retention e impressions non sono disponibili. Abilitala su "
                  "https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com",
                  flush=True)
            return {}, False
        print(f"[analytics] query aggregata fallita ({str(e)[:200]}), ripiego sulle singole")

    per_video = {}
    ok = False
    for vid in video_ids:
        try:
            resp = (
                yta.reports()
                .query(ids="channel==MINE", startDate="2020-01-01", endDate=oggi,
                       metrics=_METRICHE, filters=f"video=={vid}")
                .execute()
            )
            rows = resp.get("rows") or [[0, 0, 0, 0, 0, 0]]
            per_video[vid] = tuple(rows[0][:6])
            ok = True
        except Exception as e:
            if _api_disabilitata(e):
                return {}, False
            print(f"[analytics] errore query video {vid}: {str(e)[:200]}")
    return per_video, ok


def leggi_performance(n_video: int = 5) -> list[dict]:
    """Return performance data for the last n_video uploads."""
    creds = get_credentials()
    yt = build("youtube", "v3", credentials=creds)
    yta = build("youtubeAnalytics", "v2", credentials=creds)

    ch_resp = yt.channels().list(part="contentDetails", mine=True).execute()
    items_ch = ch_resp.get("items", [])
    if not items_ch:
        return []
    uploads = items_ch[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    items = (
        yt.playlistItems()
        .list(part="contentDetails", playlistId=uploads, maxResults=n_video)
        .execute()
        .get("items", [])
    )

    if not items:
        return []

    video_ids = [i["contentDetails"]["videoId"] for i in items]

    videos_resp = (
        yt.videos()
        .list(part="snippet,statistics,contentDetails", id=",".join(video_ids))
        .execute()
        .get("items", [])
    )

    per_video, analytics_ok = _analytics_per_video(yta, video_ids)

    results = []
    for v in videos_resp:
        vid = v["id"]
        stats = v.get("statistics", {})

        (views_ana, avg_duration, avg_view_pct,
         minutes_watched, impressions, ctr) = per_video.get(vid, (0, 0, 0, 0, 0, 0))
        # CTR reale: API lo ritorna come frazione (0.05 = 5%) → moltiplica
        if ctr:
            ctr_real = ctr * 100
        elif impressions and views_ana:
            ctr_real = (views_ana / impressions) * 100
        else:
            ctr_real = 0

        # parsing duration ISO8601 (PT8M30S -> 510 sec)
        import re as _re
        dur_str = v.get("contentDetails", {}).get("duration", "PT0S")
        m = _re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", dur_str)
        if m:
            h, mi, s = (int(g) if g else 0 for g in (m.group(1), m.group(2), m.group(3)))
            duration_seconds = h * 3600 + mi * 60 + s
        else:
            duration_seconds = 0

        results.append({
            "video_id": vid,
            "title": v["snippet"]["title"],
            "published_at": v["snippet"].get("publishedAt", ""),
            "published_hour_utc": _published_hour(v["snippet"].get("publishedAt", "")),
            "views": int(stats.get("viewCount", 0)),
            "likes": int(stats.get("likeCount", 0)),
            "comments": int(stats.get("commentCount", 0)),
            "duration_seconds": duration_seconds,
            "avg_view_duration_seconds": avg_duration,
            "avg_view_percentage": round(avg_view_pct, 2) if avg_view_pct else 0,
            "estimated_minutes_watched": minutes_watched,
            "impressions": impressions,
            "ctr_percent": round(ctr_real, 2),
            "ctr": ctr,
            # False = metriche Analytics non disponibili (API spenta o errore):
            # gli zeri qui sopra sono assenza di dato, non performance nulla
            "analytics_disponibili": analytics_ok,
        })

    return results


def _published_hour(value: str):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).hour
    except Exception:
        return None
