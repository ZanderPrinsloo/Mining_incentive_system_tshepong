"""Flask application for the Tshepong Stoping Analysis Dashboard."""
from __future__ import annotations
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_DATABASE", "STPTM4000")

import logging
import traceback

from flask import Flask, render_template, jsonify, request
from web import queries as q

log = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates")

    @app.errorhandler(Exception)
    def handle_exception(e):
        log.error("Unhandled API error:\n%s", traceback.format_exc())
        return jsonify({"error": str(e)}), 500

    @app.route("/")
    def index():
        return render_template("index.html")

    # ── Meta endpoints ────────────────────────────────────────────────────────

    @app.route("/api/periods")
    def api_periods():
        return jsonify(q.get_available_periods())

    @app.route("/api/sections")
    def api_sections():
        p_from = int(request.args.get("from", 202507))
        p_to   = int(request.args.get("to",   202606))
        return jsonify(q.get_sections(p_from, p_to))

    # ── Data endpoints ────────────────────────────────────────────────────────

    @app.route("/api/kpi")
    def api_kpi():
        p_from   = int(request.args.get("from",    202507))
        p_to     = int(request.args.get("to",      202606))
        section  = request.args.get("section",  "ALL")
        gangtype = request.args.get("gangtype",  "ALL")
        return jsonify(q.get_kpi_summary(p_from, p_to, section, gangtype))

    @app.route("/api/trend")
    def api_trend():
        p_from  = int(request.args.get("from",   202507))
        p_to    = int(request.args.get("to",     202606))
        section = request.args.get("section", "ALL")
        return jsonify(q.get_production_trend(p_from, p_to, section))

    @app.route("/api/pre_adj_trend")
    def api_pre_adj_trend():
        p_from  = int(request.args.get("from",   202507))
        p_to    = int(request.args.get("to",     202606))
        section = request.args.get("section", "ALL")
        return jsonify(q.get_pre_adj_trend(p_from, p_to, section))

    @app.route("/api/bonus_summary")
    def api_bonus_summary():
        p_from  = int(request.args.get("from",   202507))
        p_to    = int(request.args.get("to",     202606))
        section = request.args.get("section", "ALL")
        return jsonify(q.get_bonus_by_gangtype(p_from, p_to, section))

    @app.route("/api/section_bonus")
    def api_section_bonus():
        p_from = int(request.args.get("from", 202507))
        p_to   = int(request.args.get("to",   202606))
        return jsonify(q.get_bonus_by_section(p_from, p_to))

    @app.route("/api/sqm_range")
    def api_sqm_range():
        p_from  = int(request.args.get("from",   202507))
        p_to    = int(request.args.get("to",     202606))
        section = request.args.get("section", "ALL")
        return jsonify(q.get_sqm_range_analysis(p_from, p_to, section))

    @app.route("/api/gang_detail")
    def api_gang_detail():
        p_from   = int(request.args.get("from",    202507))
        p_to     = int(request.args.get("to",      202606))
        section  = request.args.get("section",  "ALL")
        gangtype = request.args.get("gangtype",  "ALL")
        return jsonify(q.get_gang_detail(p_from, p_to, section, gangtype))

    @app.route("/api/r_per_sqm")
    def api_r_per_sqm():
        p_from  = int(request.args.get("from",   202507))
        p_to    = int(request.args.get("to",     202606))
        section = request.args.get("section", "ALL")
        return jsonify(q.get_rands_per_sqm(p_from, p_to, section))

    @app.route("/api/simulation_data")
    def api_simulation_data():
        p_from  = int(request.args.get("from",   202507))
        p_to    = int(request.args.get("to",     202606))
        section = request.args.get("section", "ALL")
        return jsonify(q.get_simulation_data(p_from, p_to, section))

    @app.route("/api/forecast_data")
    def api_forecast_data():
        p_from  = int(request.args.get("from",   202507))
        p_to    = int(request.args.get("to",     202606))
        section = request.args.get("section", "ALL")
        return jsonify(q.get_forecast_data(p_from, p_to, section))

    @app.route("/api/period_performance")
    def api_period_performance():
        p_from  = int(request.args.get("from",   202507))
        p_to    = int(request.args.get("to",     202606))
        section = request.args.get("section", "ALL")
        return jsonify(q.get_period_performance(p_from, p_to, section))

    @app.route("/api/gang_production_detail")
    def api_gang_production_detail():
        gang   = request.args.get("gang", "")
        p_from = int(request.args.get("from", 202507))
        p_to   = int(request.args.get("to",   202606))
        return jsonify(q.get_gang_production_detail(gang, p_from, p_to))

    return app
