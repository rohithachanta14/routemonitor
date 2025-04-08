"""Correlation Matrix page — which prefixes fail together."""
import httpx
import pandas as pd
import plotly.express as px
import streamlit as st


def render(client) -> None:
    """Render the Correlation Matrix page."""
    st.title("🔗 Correlation Matrix")
    st.caption("Which prefixes fail together? Correlation > 0.8 → likely shared link")

    col1, col2 = st.columns(2)
    with col1:
        time_range = st.selectbox("Time Range", options=["24h", "7d", "30d"], index=1)
    with col2:
        top_n = st.slider(
            "Top N Prefixes (by flap volume)", min_value=5, max_value=50, value=20
        )

    if not st.button("Compute Correlation"):
        return

    try:
        result = client.get_correlation(time_range=time_range, top_n_prefixes=top_n)
    except httpx.HTTPStatusError as e:
        st.error(f"API error {e.response.status_code}: {e.response.text}")
        return
    except httpx.ConnectError:
        st.error("Cannot connect to RouteMonitor API. Is Docker running?")
        return

    matrix = result.get("matrix", {})
    if not matrix:
        st.warning(
            "No correlation data available for this time range. "
            "Make sure InfluxDB has route_stats data with prefix tags."
        )
        return

    df = pd.DataFrame(matrix).fillna(0)
    if df.empty:
        st.warning("Correlation matrix is empty for this time range.")
        return

    fig = px.imshow(
        df,
        color_continuous_scale="RdBu_r",
        zmin=-1,
        zmax=1,
        title=f"Prefix Failure Correlation ({time_range})",
        labels={"color": "Pearson r"},
    )
    fig.update_layout(height=600)
    fig.update_xaxes(tickangle=45)
    st.plotly_chart(fig, use_container_width=True)

    st.caption(
        "Values close to **+1.0** indicate prefixes that fail simultaneously "
        "(likely shared upstream link)."
    )

    st.subheader("Highest Correlated Pairs")
    pairs = []
    cols = list(df.columns)
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            r = df.iloc[i, j]
            pairs.append(
                {
                    "prefix_a": cols[i],
                    "prefix_b": cols[j],
                    "correlation": round(float(r), 4),
                    "risk": "⚠️ Shared link?" if abs(r) > 0.8 else "Normal",
                }
            )

    pairs_df = pd.DataFrame(pairs).sort_values("correlation", key=abs, ascending=False)
    st.dataframe(pairs_df.head(20), use_container_width=True)
