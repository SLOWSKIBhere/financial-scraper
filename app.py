import os
import subprocess
import streamlit as st

st.set_page_config(page_title="Financial Digest Hub", layout="wide", page_icon="📈")

st.title("🤖 Autonomous Financial Digest Dashboard")
st.write("Review synthesized signals from your RSS feeds and community pipelines.")

DIGEST_PATH = "agent_outputs/financial_digest.md"

st.sidebar.header("Pipeline Controls")
if st.sidebar.button("Rerun Multi-Agent Pipeline", type="primary"):
    with st.sidebar.spinner("Running agents concurrently..."):
        subprocess.run(["python", "agentic_pipeline.py"], check=True)
        st.sidebar.success("Pipeline refreshed successfully!")
        st.rerun()

st.markdown("---")

if os.path.exists(DIGEST_PATH):
    st.subheader("📝 Latest Ingested Market Report")
    with open(DIGEST_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    st.markdown(content)
else:
    st.info("No active report file found yet. Use the sidebar button to fire up your processing agents.")
