"""Device Health page — BGP speaker uptime, update rate, flap stats."""
import httpx
import pandas as pd
import plotly.express as px
import streamlit as st

from dashboard.utils.formatting import format_duration_seconds


def render(client) -> None:
    """Render the Device Health page."""
    st.title("🖥️ Device Health")
    st.caption("BGP speaker uptime, update rate, and error statistics")

    col_refresh, col_auto = st.columns([1, 3])
    with col_refresh:
        if st.button("🔄 Refresh"):
            st.cache_data.clear()
            st.rerun()
    with col_auto:
        auto_refresh = st.checkbox("Auto-refresh every 30s", value=False)

    try:
        speakers = client.list_speakers()
    except httpx.HTTPStatusError as e:
        st.error(f"API error {e.response.status_code}: {e.response.text}")
        return
    except httpx.ConnectError:
        st.error("Cannot connect to RouteMonitor API. Is Docker running?")
        return

    if not speakers:
        st.info("No BGP speakers registered yet.")
        return

    total = len(speakers)
    connected = sum(1 for s in speakers if s["status"] == "CONNECTED")
    degraded = sum(1 for s in speakers if s["status"] == "DEGRADED")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Speakers", total)
    c2.metric("Connected", connected)
    c3.metric("Degraded", degraded, delta=f"-{degraded}" if degraded else None)
    c4.metric("Disconnected", total - connected - degraded)

    st.divider()

    status_icons = {
        "CONNECTED": "🟢",
        "DEGRADED": "🟡",
        "DISCONNECTED": "🔴",
    }

    for speaker in speakers:
        sid = speaker["id"]
        try:
            status = client.get_speaker_status(sid)
        except httpx.HTTPStatusError:
            status = {}

        icon = status_icons.get(speaker["status"], "⚪")
        with st.expander(
            f"{icon} **{speaker['hostname']}** — ASN {speaker['local_asn']} — {speaker['status']}",
            expanded=False,
        ):
            m1, m2, m3, m4 = st.columns(4)
            m1.metric(
                "Uptime",
                format_duration_seconds(status.get("connected_for_seconds", 0)),
            )
            m2.metric(
                "Routes advertised (24h)", status.get("routes_advertised_24h", "–")
            )
            m3.metric("Routes withdrawn (24h)", status.get("routes_withdrawn_24h", "–"))
            m4.metric("Flap rate", f"{status.get('current_flap_rate', 0):.1f}/5m")

            try:
                stats = client.get_route_stats(sid, time_range="24h")
                points = stats.get("data_points", [])
                if points:
                    df = pd.DataFrame(points)
                    df["time"] = pd.to_datetime(df["time"])
                    fig = px.line(
                        df,
                        x="time",
                        y="flap_count",
                        title=f"Flap Rate — {speaker['hostname']}",
                        labels={"flap_count": "Flap Count", "time": "Time"},
                        color_discrete_sequence=["#e74c3c"],
                    )
                    fig.update_layout(height=250, margin=dict(t=40, b=20))
                    st.plotly_chart(fig, use_container_width=True)
            except httpx.HTTPStatusError:
                st.caption("Could not load flap rate chart.")

    if auto_refresh:
        st.caption("Auto-refresh: click Refresh or reload the page to update.")
