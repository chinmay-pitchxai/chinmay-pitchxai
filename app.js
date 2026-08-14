/**
 * Voice Calling Dashboard - Central Controller
 */

(function() {
    'use strict';

    // --- State variables ---
    window.appState = {
        currentRole: 'Vernika', // Technopolis — Solitaire Unity
        currentSandbox: 1,        // Sandboxes 1-4 (dashboard)
        campaignSandbox: 1,       // Selected sandbox for campaigns view
        currentFilter: 'all',     // Table filter
        tableSearch: '',          // Table search query
        allLeads: [],
        inboundCallbacks: [],
        currentView: 'dashboard'  // 'dashboard', 'campaigns', 'make-a-call', 'config'
    };

    // Global sandbox filter for leads (0=all, 1-4=specific sandbox)
    let currentSandboxFilter = 0;
    console.log('🚀 app.js v2.0 — 4-sandbox architecture loaded | currentSandboxFilter=', currentSandboxFilter);

    /* Production state is loaded exclusively from backend APIs. Legacy demo
       generators are retained only in source history, never in this bundle. */
    /*
    const names = [
        "Puneetha", "Gaus", "Pandrish Rao", "Ananya Sharma", "Rahul Verma", 
        "Deepak Patel", "Siddharth Sen", "Megha Rao", "Karan Johar", "Ritika Sen",
        "Rohan Gupta", "Aditi Nair", "Vikram Malhotra", "Kavita Reddy", "Sanjay Dutt",
        "Neha Kapoor", "Abhishek Shah", "Shreya Ghoshal", "Tanmay Bhat", "Alia Bhatt",
        "Varun Dhawan", "Kiara Advani", "Ranbir Kapoor", "Katrina Kaif", "Vicky Kaushal",
        "Rajkummar Rao", "Shraddha Kapoor", "Ayushmann Khurrana", "Kriti Sanon", "Kartik Aaryan"
    ];

    const companies = ["Technopolis Solitaire Unity", "Technopolis Luxury Residences", "Technopolis Commercial Space", "Eco-Friendly Farms"];
    const segments = ["Premium Apartments Outbound", "Commercial Space Inbound", "Premium Residences Phase 2", "Agri Plot Calling"];
    const failureReasons = ["All 3 attempts used", "Busy tone", "Invalid number", "Call dropped by carrier", "No answer after 45s", "User rejected incoming"];
    
    const transcripts = {
        interested: [
            { role: 'agent', text: "Hello! Am I speaking with {{name}}?", time: "0:02" },
            { role: 'user', text: "Yes, this is {{name}}. Who is this?", time: "0:05" },
            { role: 'agent', text: "Hi {{name}}! I am calling from Technopolis Solitaire Unity, our premium ready-to-move apartment project in Kondapur. I noticed you recently inquired about premium residences in Hyderabad. Is this a good time to talk?", time: "0:16" },
            { role: 'user', text: "Oh yes. I'm actually looking for a 3BHK apartment. What is the starting price?", time: "0:23" },
            { role: 'agent', text: "Our luxury 3BHK apartments start at 1.34 Crores. They feature spacious layouts, modern amenities, and a massive 32,000 sq.ft. clubhouse. Would you be interested in a site visit this coming weekend?", time: "0:38" },
            { role: 'user', text: "1.8 Crores sounds within my budget. Can you send me the brochure on WhatsApp? And yes, I can schedule a site visit for Sunday morning.", time: "0:47" },
            { role: 'agent', text: "Absolutely! I will trigger the digital brochure and location details to your WhatsApp number immediately. I will also lock in your site visit for Sunday at 11:00 AM. Thank you so much, have a wonderful day!", time: "1:02" },
            { role: 'user', text: "Perfect, thank you. Talk to you soon.", time: "1:05" }
        ],
        not_interested: [
            { role: 'agent', text: "Hello! Am I speaking with {{name}}?", time: "0:02" },
            { role: 'user', text: "Yes, tell me.", time: "0:04" },
            { role: 'agent', text: "Hi {{name}}! I'm calling from Technopolis Solitaire Unity, premium apartments in Kondapur. I see that you're looking for residential projects in Hyderabad.", time: "0:12" },
            { role: 'user', text: "No, I am not interested. I already bought an apartment last month. Please don't call again.", time: "0:19" },
            { role: 'agent', text: "Understood. Thank you for your time, {{name}}. Have a nice day.", time: "0:24" }
        ],
        callback: [
            { role: 'agent', text: "Hello! Am I speaking with {{name}}?", time: "0:02" },
            { role: 'user', text: "Yes, but I am in a client meeting right now. Can you call me back later?", time: "0:07" },
            { role: 'agent', text: "Of course! I completely understand. Would today evening around 6:00 PM be a better time to call you back?", time: "0:14" },
            { role: 'user', text: "Yes, 6:00 PM is fine. Call me then.", time: "0:17" },
            { role: 'agent', text: "Perfect, I will mark a callback for 6:00 PM. Have a productive meeting!", time: "0:22" }
        ]
    };

    const remarks = [
        "Inquired about premium 3BHK apartments. Requested site visit Sunday morning 11 AM.",
        "Interested in commercial office space for startup. Asked for payment plans and brochure.",
        "Looking for investment options in Kondapur. Highly interested in Solitaire Unity.",
        "Requested floor plans for 3BHK apartments. Scheduled site visit Saturday evening.",
        "Interested, but budget is capped at 1.5Cr. Asked if negotiation is possible on booking."
    ];

    function generateMockLeads() {
        const leads = [];
        let idCounter = 1;
        const now = new Date();

        // Ensure we generate enough leads for realistic statistics
        // Sandbox 1: 45 leads, Sandbox 2: 30 leads, Sandbox 3: 25 leads, Sandbox 4: 20 leads = 120 total
        const sandboxCounts = [55, 38, 28, 22];

        sandboxCounts.forEach((count, sIdx) => {
            const sandboxId = sIdx + 1;
            for (let i = 0; i < count; i++) {
                const name = names[Math.floor(Math.random() * names.length)] + " " + String.fromCharCode(65 + Math.floor(Math.random() * 26)) + ".";
                const phone = "+91" + (7000000000 + Math.floor(Math.random() * 2999999999));
                const email = name.toLowerCase().replace(/[^a-z]/g, '') + "@gmail.com";
                const company = companies[Math.floor(Math.random() * companies.length)];
                const segment = segments[Math.floor(Math.random() * segments.length)];
                
                // Determine lead state based on probabilities
                const rand = Math.random();
                let status = "pending";
                let disposition = "Answered";
                let error = "—";
                let rating = "—";
                let summary = "";
                let transcriptData = [];
                let duration = 0;
                let whatsapp = false;
                let siteVisit = false;

                // Call times spread across last 7 days
                const callDayDiff = Math.floor(Math.random() * 7);
                const callHour = 9 + Math.floor(Math.random() * 11); // 9 AM to 7 PM
                const callMin = Math.floor(Math.random() * 60);
                const callDate = new Date(now.getTime() - callDayDiff * 24 * 60 * 60 * 1000);
                callDate.setHours(callHour, callMin, 0, 0);

                if (rand < 0.35) { // 35% Interested (Conversion)
                    status = "completed";
                    disposition = "Interested";
                    rating = Math.random() > 0.4 ? (Math.random() > 0.5 ? 5 : 4) : 3;
                    summary = remarks[Math.floor(Math.random() * remarks.length)];
                    duration = 45 + Math.floor(Math.random() * 75);
                    whatsapp = true;
                    siteVisit = Math.random() > 0.4;
                    // Custom transcripts
                    transcriptData = JSON.parse(JSON.stringify(transcripts.interested))
                        .map(t => { t.text = t.text.replace(/{{name}}/g, name.split(" ")[0]); return t; });
                } else if (rand < 0.65) { // 30% Not Interested
                    status = "completed";
                    disposition = "Not Interested";
                    rating = Math.random() > 0.7 ? 3 : (Math.random() > 0.5 ? 2 : 1);
                    summary = "Not interested. Lead already purchased property elsewhere / outside budget.";
                    duration = 15 + Math.floor(Math.random() * 25);
                    transcriptData = JSON.parse(JSON.stringify(transcripts.not_interested))
                        .map(t => { t.text = t.text.replace(/{{name}}/g, name.split(" ")[0]); return t; });
                } else if (rand < 0.80) { // 15% Callback / Call Later
                    status = "completed";
                    disposition = "Call Later";
                    rating = 3;
                    summary = "Lead requested callback. Busy in meeting / driving.";
                    duration = 10 + Math.floor(Math.random() * 15);
                    transcriptData = JSON.parse(JSON.stringify(transcripts.callback))
                        .map(t => { t.text = t.text.replace(/{{name}}/g, name.split(" ")[0]); return t; });
                } else if (rand < 0.95) { // 15% Failed / No Answer
                    status = "failed";
                    disposition = "Failed";
                    error = failureReasons[Math.floor(Math.random() * failureReasons.length)];
                    duration = 0;
                    summary = "Why this failed: " + error;
                } else { // 5% Pending
                    status = "pending";
                    disposition = "Pending";
                    duration = 0;
                    summary = "Lead added, call pending dispatch.";
                }

                leads.push({
                    id: idCounter++,
                    name: name,
                    phone: phone,
                    email: email,
                    company: company,
                    segment: segment,
                    status: status,
                    disposition: disposition,
                    error: error,
                    rating: rating,
                    summary: summary,
                    transcript: transcriptData,
                    duration_sec: duration,
                    created_at: Math.floor(callDate.getTime() / 1000) - 3600, // added 1hr prior
                    start_time: status !== "pending" ? Math.floor(callDate.getTime() / 1000) : null,
                    called_at_iso: status !== "pending" ? callDate.toISOString() : null,
                    sandbox: sandboxId,
                    whatsapp_sent: whatsapp,
                    site_visit_scheduled: siteVisit
                });
            }
        });

        // Add the hardcoded rows from template specifically to Sandbox 1 to maintain visual alignment
        const hardcoded = [
            {
                id: idCounter++,
                name: "Puneetha",
                phone: "+918904635217",
                email: "puneetha@gmail.com",
                company: "Technopolis Solitaire Unity",
                segment: "Premium Apartments Outbound",
                status: "completed",
                disposition: "Interested",
                error: "—",
                rating: 2,
                summary: "The agent introduced Technopolis Solitaire Unity and shared pricing details. Callee responded positively and showed interest in a site visit.",
                transcript: JSON.parse(JSON.stringify(transcripts.interested)).map(t => { t.text = t.text.replace(/{{name}}/g, "Puneetha"); return t; }),
                duration_sec: 72,
                created_at: Math.floor(new Date("2026-08-03T17:40:00").getTime() / 1000),
                start_time: Math.floor(new Date("2026-08-03T17:43:00").getTime() / 1000),
                called_at_iso: new Date("2026-08-03T17:43:00").toISOString(),
                sandbox: 1,
                whatsapp_sent: true,
                site_visit_scheduled: false
            },
            {
                id: idCounter++,
                name: "Gaus",
                phone: "+919604821186",
                email: "gaus@gmail.com",
                company: "Technopolis Solitaire Unity",
                segment: "Premium Apartments Outbound",
                status: "failed",
                disposition: "Failed",
                error: "All 3 attempts used",
                rating: "—",
                summary: "Why this failed: All 3 attempts used",
                transcript: [],
                duration_sec: 0,
                created_at: Math.floor(new Date("2026-08-03T17:40:00").getTime() / 1000),
                start_time: Math.floor(new Date("2026-08-03T17:43:00").getTime() / 1000),
                called_at_iso: new Date("2026-08-03T17:43:00").toISOString(),
                sandbox: 1,
                whatsapp_sent: false,
                site_visit_scheduled: false
            },
            {
                id: idCounter++,
                name: "Pandrish Rao",
                phone: "9880492902",
                email: "pandrish@gmail.com",
                company: "Technopolis Solitaire Unity",
                segment: "Commercial Inbound",
                status: "inbound",
                disposition: "Callback",
                error: "—",
                rating: "—",
                summary: "No summary yet - Inbound callback matched to lead.",
                transcript: [],
                duration_sec: 0,
                created_at: Math.floor(new Date("2026-08-03T17:40:00").getTime() / 1000),
                start_time: Math.floor(new Date("2026-08-03T17:43:00").getTime() / 1000),
                called_at_iso: new Date("2026-08-03T17:43:00").toISOString(),
                sandbox: 1,
                whatsapp_sent: false,
                site_visit_scheduled: false,
                is_inbound: true
            }
        ];

        return leads.concat(hardcoded);
    }

    */
    // --- Inbound callbacks are backend-only ---
    /*
    function generateInboundCallbacks() {
        return [
            { from: "+919880492902", to: "+918047091211", time: "3 Aug, 05:43 pm", name: "Pandrish Rao", live: "Yes" },
            { from: "+919108172635", to: "+918047091211", time: "3 Aug, 04:12 pm", name: "Kiran Kumar", live: "No" },
            { from: "+917204812390", to: "+918047091211", time: "2 Aug, 06:20 pm", name: "Unrecognized", live: "No" }
        ];
    }

    // ── Real-data loading from the FastAPI backend ──────────────────────────
    */
    window.apiBase = window.apiBase || (window.location.origin || "");

    // ── Auth helpers (shared with the /console bundle) ──────────────────────
    window.dashToken = function() { return localStorage.getItem('vernika_token') || ''; };
    window.dashAuthHeaders = function() { return { 'Authorization': `Bearer ${window.dashToken()}`, 'Content-Type': 'application/json' }; };
    window.dashRoleForApi = function() { return localStorage.getItem('vernika_role') || 'sales_1'; };
    window.dashEnsureAuth = function() {
        if (window.dashToken()) return true;
        window.showToast && window.showToast('Please sign in first (redirecting to login…).', 'error');
        setTimeout(() => { window.location.href = '/login'; }, 900);
        return false;
    };

    // Lead fields the UI expects (kept in sync with backend /api/dashboard/leads):
    // id, name, phone, email, company, segment, status, disposition, error,
    // rating, summary, transcript, duration_sec, created_at, start_time,
    // called_at_iso, sandbox, whatsapp_sent, site_visit_scheduled.
    function normalizeApiLead(l) {
        return {
            id: l.id,
            name: l.name || "",
            phone: l.phone || "",
            email: l.email || "",
            company: l.company || "",
            segment: l.segment || "",
            status: l.status || "pending",
            disposition: l.disposition || "Pending",
            error: l.error || "—",
            rating: (typeof l.rating === "number") ? l.rating : "—",
            summary: l.summary || "No summary yet.",
            transcript: l.transcript || [],
            duration_sec: l.duration_sec || 0,
            created_at: l.created_at || 0,
            start_time: l.start_time || null,
            called_at_iso: l.called_at_iso || null,
            sandbox: l.sandbox || 1,
            whatsapp_sent: !!l.whatsapp_sent,
            site_visit_scheduled: !!l.site_visit_scheduled
        };
    }

    // Set sandbox filter for leads view (0=all, 1-4=specific sandbox)
    window.setSandboxFilter = function(num) {
        currentSandboxFilter = num;
        window.loadRealLeads();
    };

    window.loadRealLeads = async function() {
        try {
            let url = window.apiBase + `/api/dashboard/leads?limit=100000&role=${encodeURIComponent(window.dashRoleForApi())}`;
            if (currentSandboxFilter > 0) {
                url += "&sandbox=" + currentSandboxFilter;
            }
            const res = await fetch(url, { cache: "no-store" });
            if (!res.ok) throw new Error("HTTP " + res.status);
            const data = await res.json();
            const leads = (data && data.leads) || [];
            if (leads.length > 0) {
                window.appState.allLeads = leads.map(normalizeApiLead);
                window.appState.inboundCallbacks = leads.filter(l => l.status === 'inbound').map(normalizeApiLead);
                return true;
            }
        } catch (e) {
            console.warn("Dashboard: real-data load failed.", e);
        }
        // Production dashboard must never invent leads when the API is empty
        // or temporarily unavailable.
        window.appState.allLeads = [];
        window.appState.inboundCallbacks = [];
        return false;
    };

    // Initialize state database (real data async, mock as fallback).
    window.appState.allLeads = [];
    window.appState.inboundCallbacks = [];
    window.appState.dataLoadedFromApi = false;

    // --- Helper Utilities ---
    window.isCalled = function(lead) {
        return lead.status !== 'pending' && lead.status !== 'inbound';
    };

    window.isFailed = function(lead) {
        const s = (lead.status || '').toLowerCase();
        const d = (lead.disposition || '').toLowerCase();
        return s === 'failed' || s === 'no answer' || s === 'no_answer' || s === 'no response' || s === 'no_response'
            || d === 'failed' || d === 'no answer' || d === 'no response';
    };

    window.effectiveDispo = function(lead) {
        return lead.disposition;
    };

    window.prettyStatus = function(status) {
        if (status === 'completed') return 'Answered';
        if (status === 'failed') return 'No Answer';
        if (status === 'inbound') return 'Inbound';
        return status;
    };

    // --- Copy Toast Function ---
    window.showToast = function(message, type = 'success') {
        const host = document.getElementById('toast-host');
        if (!host) return;

        const toast = document.createElement('div');
        toast.className = 'toast show';
        toast.textContent = message;

        // Custom styling based on type
        if (type === 'error') {
            toast.style.borderLeft = '4px solid #ba1a1a';
        } else if (type === 'info') {
            toast.style.borderLeft = '4px solid #00629f';
        } else {
            toast.style.borderLeft = '4px solid #006a65';
        }

        host.appendChild(toast);

        setTimeout(() => {
            toast.classList.remove('show');
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    };

    // --- Open/Close Modal Utilities ---
    window.openModal = function(id) {
        const el = document.getElementById(id);
        if (el) {
            el.classList.add('active');
            document.body.style.overflow = 'hidden';
        }
    };

    window.closeModal = function(id) {
        const el = document.getElementById(id);
        if (el) {
            el.classList.remove('active');
            document.body.style.overflow = '';
        }
    };

    // Global hook for kpi_modal.js
    window.allLeads = window.appState.allLeads;
    window.lastCampaignSnapshot = {
        texts: { 'stat-called': '0' },
        disposition_counts: {}
    };

    // --- Re-calculate KPIs ---
    window.calculateKpis = function() {
        const sandboxLeads = window.appState.allLeads.filter(l => l.sandbox === window.appState.currentSandbox);
        const calledLeads = sandboxLeads.filter(isCalled);
        
        const total = sandboxLeads.length;
        const called = calledLeads.length;
        const interested = sandboxLeads.filter(l => l.disposition === 'Interested').length;
        const convRate = called > 0 ? Math.round((interested / called) * 100) : 0;
        
        // Secondary KPIs
        const whatsapp = sandboxLeads.filter(l => l.whatsapp_sent).length;
        const siteVisit = sandboxLeads.filter(l => l.site_visit_scheduled).length;
        const notInterested = sandboxLeads.filter(l => l.disposition === 'Not Interested').length;
        const callbacks = sandboxLeads.filter(l => l.disposition === 'Call Later' || l.disposition === 'Callback').length;

        // Calculate Average Rating
        const ratedLeads = sandboxLeads.filter(l => typeof l.rating === 'number');
        const avgRating = ratedLeads.length > 0 
            ? (ratedLeads.reduce((s, l) => s + l.rating, 0) / ratedLeads.length).toFixed(1)
            : '—';

        // Update pipeline visualization
         const pendingCount = sandboxLeads.filter(l => l.status === 'pending').length;
         const dialingCount = sandboxLeads.filter(l => l.status === 'dialing').length;
         const answeredCount = sandboxLeads.filter(l => l.status === 'completed').length;
         const failedCount = sandboxLeads.filter(l => l.status === 'failed' || isFailed(l)).length;
         const interestedCount = sandboxLeads.filter(l => l.disposition === 'Interested').length;
         const callbackCount = sandboxLeads.filter(l => l.disposition === 'Call Later' || l.disposition === 'Callback').length;
         const siteVisitCount = sandboxLeads.filter(l => l.site_visit_scheduled).length;
         const sandboxCounts = {1:0,2:0,3:0,4:0};
         sandboxLeads.forEach(l => { sandboxCounts[l.sandbox] = (sandboxCounts[l.sandbox]||0) + 1; });
         setDomText('pipe-upload-count', total);
         setDomText('pipe-pending-count', pendingCount);
         setDomText('pipe-dialing-count', dialingCount);
         setDomText('pipe-answered-count', answeredCount);
         setDomText('pipe-failed-count', failedCount);
         setDomText('pipe-interested-count', interestedCount);
         setDomText('pipe-callback-count', callbackCount);
         setDomText('pipe-sitevisit-count', siteVisitCount);
         setDomText('sb1-count', sandboxCounts[1]||0);
         setDomText('sb2-count', sandboxCounts[2]||0);
         setDomText('sb3-count', sandboxCounts[3]||0);
         setDomText('sb4-count', sandboxCounts[4]||0);

         // Update Global state for kpi_modal access
         window.allLeads = sandboxLeads;
         window.lastCampaignSnapshot.texts['stat-called'] = called.toString();
         window.lastCampaignSnapshot.disposition_counts = {
             'Failed': sandboxLeads.filter(isFailed).length
         };

         return {
             total, called, interested, convRate,
             whatsapp, siteVisit, notInterested, callbacks, avgRating
         };
    };

    // --- Update Dashboard DOM ---
    window.updateDashboardUI = function() {
        const kpis = window.calculateKpis();

        // Update Card values
        setDomText('kpi-total-val', kpis.total.toLocaleString());
        setDomText('kpi-called-val', kpis.called.toLocaleString());
        setDomText('kpi-interested-val', kpis.interested.toLocaleString());
        setDomText('kpi-conv-val', kpis.convRate + '%');

        setDomText('kpi-whatsapp-val', kpis.whatsapp.toLocaleString());
        setDomText('kpi-sitevisit-val', kpis.siteVisit.toLocaleString());
        setDomText('kpi-notinterested-val', kpis.notInterested.toLocaleString());
        setDomText('kpi-callbacks-val', kpis.callbacks.toLocaleString());

        // Update charts & table
        window.updateDashboardCharts();
        window.renderCallLogsTable();
    };

    function setDomText(id, val) {
        const el = document.getElementById(id);
        if (el) el.textContent = val;
    }

    // --- Update Dashboard Interactive Charts ---
    window.updateDashboardCharts = function() {
        const sandboxLeads = window.appState.allLeads.filter(l => l.sandbox === window.appState.currentSandbox);
        const called = sandboxLeads.filter(isCalled);

        // 1. Engagement Timeline (Area Chart)
        // Group calls and interested leads by day of the week (last 7 days)
        const days = ['Sat', 'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri'];
        const callCounts = [0, 0, 0, 0, 0, 0, 0];
        const interestedCounts = [0, 0, 0, 0, 0, 0, 0];

        called.forEach(l => {
            if (l.called_at_iso) {
                const date = new Date(l.called_at_iso);
                const day = date.toLocaleDateString([], { weekday: 'short' });
                const idx = days.indexOf(day);
                if (idx !== -1) {
                    callCounts[idx]++;
                    if (l.disposition === 'Interested') {
                        interestedCounts[idx]++;
                    }
                }
            }
        });

        // Regenerate Area paths for SVG
        // SVG size: 800 x 250
        // Mapping counts to height (max height 200, representing e.g. 15 calls)
        const maxVal = Math.max(...callCounts, 5); // Avoid division by zero, scale relative to 5 min
        
        let pathCalls = "M0,230";
        let pathCallsFill = "M0,230";
        let pathInt = "M0,230";
        let pathIntFill = "M0,230";

        const points = 7;
        const xStep = 800 / (points - 1);

        for (let i = 0; i < points; i++) {
            const x = i * xStep;
            // High y means low visual coordinate
            const yCall = 230 - (callCounts[i] / maxVal) * 180;
            const yInt = 230 - (interestedCounts[i] / maxVal) * 180;

            pathCalls += ` L${x},${yCall}`;
            pathCallsFill += ` L${x},${yCall}`;
            pathInt += ` L${x},${yInt}`;
            pathIntFill += ` L${x},${yInt}`;

            // Update markers for peak or Tuesday/Wed
            if (i === 3) { // Wed (representing mid-week)
                const cMarker = document.getElementById('chart-marker-calls');
                const iMarker = document.getElementById('chart-marker-int');
                if (cMarker) { cMarker.setAttribute('cx', x); cMarker.setAttribute('cy', yCall); }
                if (iMarker) { iMarker.setAttribute('cx', x); iMarker.setAttribute('cy', yInt); }
            }
        }

        // Store for hover access
        window.chartCurrentData = {
            days: days,
            calls: callCounts,
            interested: interestedCounts,
            maxVal: maxVal
        };

        pathCallsFill += " L800,230 L800,250 L0,250 Z";
        pathIntFill += " L800,230 L800,250 L0,250 Z";

        const svgCallsFill = document.getElementById('svg-calls-fill');
        const svgCallsLine = document.getElementById('svg-calls-line');
        const svgIntFill = document.getElementById('svg-int-fill');
        const svgIntLine = document.getElementById('svg-int-line');

        if (svgCallsFill) svgCallsFill.setAttribute('d', pathCallsFill);
        if (svgCallsLine) svgCallsLine.setAttribute('d', pathCalls);
        if (svgIntFill) svgIntFill.setAttribute('d', pathIntFill);
        if (svgIntLine) svgIntLine.setAttribute('d', pathInt);

        // Update Y Axis labels based on maxVal
        const yLabels = document.querySelectorAll('#chart-y-axis span');
        if (yLabels.length === 5) {
            yLabels[0].textContent = Math.round(maxVal).toString();
            yLabels[1].textContent = Math.round(maxVal * 0.75).toString();
            yLabels[2].textContent = Math.round(maxVal * 0.5).toString();
            yLabels[3].textContent = Math.round(maxVal * 0.25).toString();
            yLabels[4].textContent = '0';
        }

        // 2. Outcome Distribution (Donut Chart)
        const interested = sandboxLeads.filter(l => l.disposition === 'Interested').length;
        const failed = sandboxLeads.filter(isFailed).length;
        const callbacks = sandboxLeads.filter(l => l.disposition === 'Call Later' || l.disposition === 'Callback').length;
        const answered = sandboxLeads.filter(l => l.disposition === 'Answered').length;
        const totalOutcomes = interested + failed + callbacks + answered || 1;

        // Store donut details on window for hover
        window.donutCurrentData = {
            interested: interested,
            failed: failed,
            callbacks: callbacks,
            answered: answered,
            total: interested + failed + callbacks + answered
        };

        // Donut segments (radius 40, circumference = 2 * PI * 40 = 251.2)
        const pctInt = (interested / totalOutcomes) * 251.2;
        const pctFail = (failed / totalOutcomes) * 251.2;
        const pctCbk = (callbacks / totalOutcomes) * 251.2;
        const pctAns = (answered / totalOutcomes) * 251.2;

        const seg1 = document.getElementById('donut-seg-1');
        const seg2 = document.getElementById('donut-seg-2');
        const seg3 = document.getElementById('donut-seg-3');
        const seg4 = document.getElementById('donut-seg-4');
        const centerCount = document.getElementById('donut-center-count');

        if (centerCount) centerCount.textContent = (interested + failed + callbacks + answered).toLocaleString();

        if (seg1) { seg1.setAttribute('stroke-dasharray', `${pctInt} 251.2`); }
        if (seg2) { seg2.setAttribute('stroke-dasharray', `${pctFail} 251.2`); seg2.setAttribute('stroke-dashoffset', `-${pctInt}`); }
        if (seg3) { seg3.setAttribute('stroke-dasharray', `${pctCbk} 251.2`); seg3.setAttribute('stroke-dashoffset', `-${pctInt + pctFail}`); }
        if (seg4) { seg4.setAttribute('stroke-dasharray', `${pctAns} 251.2`); seg4.setAttribute('stroke-dashoffset', `-${pctInt + pctFail + pctCbk}`); }

        // 3. Hourly Distribution (Bar Chart)
        // Group calls into intervals
        const hours = Array(12).fill(0); // 12 intervals of 2 hours
        called.forEach(l => {
            if (l.called_at_iso) {
                const hour = new Date(l.called_at_iso).getHours();
                const idx = Math.floor(hour / 2);
                if (idx >= 0 && idx < 12) {
                    hours[idx]++;
                }
            }
        });

        // Store hourly details on window for hover
        window.barCurrentData = {
            hours: hours
        };

        const maxHr = Math.max(...hours, 1);
        const bars = document.querySelectorAll('.chart-bar');
        bars.forEach((bar, idx) => {
            if (idx < 12) {
                const pct = (hours[idx] / maxHr) * 95;
                bar.style.height = `${pct}%`;
            }
        });
    };

    // --- Date Filter Helper ---
     window.clearDateFilter = function() {
         const fromEl = document.getElementById('filter-date-from');
         const toEl = document.getElementById('filter-date-to');
         if (fromEl) fromEl.value = '';
         if (toEl) toEl.value = '';
         window.renderCallLogsTable();
     };

    // --- Hourly Distribution Filter ---
    window.filterHourlyDistribution = function(filterType) {
        const sandboxLeads = window.appState.allLeads.filter(l => l.sandbox === window.appState.currentSandbox);
        let called = sandboxLeads.filter(isCalled);

        // Apply disposition filter on top of time filter
        const f = window.appState.currentFilter;
        if (f === 'Interested') {
            called = called.filter(l => l.disposition === 'Interested');
        } else if (f === 'Not Interested') {
            called = called.filter(l => l.disposition === 'Not Interested');
        } else if (f === 'Callback') {
            called = called.filter(l => l.disposition === 'Call Later' || l.disposition === 'Callback');
        } else if (f === 'Failed') {
            called = called.filter(isFailed);
        }

        let filteredCalled = called;
        let title = 'All Hours';

        const RANGES = {
            morning:   { label: 'Morning (6-9AM)',   lo: 6,  hi: 9  },
            lateam:    { label: 'Late AM (9-12PM)',  lo: 9,  hi: 12 },
            afternoon: { label: 'Afternoon (12-3PM)', lo: 12, hi: 15 },
            evening:   { label: 'Evening (3-6PM)',   lo: 15, hi: 18 },
            night:     { label: 'Night (6-9PM)',     lo: 18, hi: 21 },
        };

        if (RANGES[filterType]) {
            const r = RANGES[filterType];
            filteredCalled = called.filter(l => {
                if (!l.called_at_iso) return false;
                const hour = new Date(l.called_at_iso).getHours();
                return hour >= r.lo && hour < r.hi;
            });
            title = r.label;
        }

        // Highlight the active hourly-range button
        document.querySelectorAll('.hourly-range-btn').forEach(btn => {
            const btnId = btn.id || '';
            const matchId = filterType === 'all' ? 'hourly-btn-all'
                : `hourly-btn-${filterType}`;
            if (btnId === matchId) {
                btn.className = btn.className.replace(/border-outline-variant/g, 'border-primary')
                    .replace(/text-on-surface-variant/g, 'text-primary')
                    + ' bg-primary-fixed/20';
            } else {
                btn.className = btn.className.replace(/border-primary/g, 'border-outline-variant')
                    .replace(/text-primary/g, 'text-on-surface-variant')
                    .replace(/\s*bg-primary-fixed\/20/g, '');
            }
        });

        // Regenerate the hourly chart with filtered data
        const hours = Array(12).fill(0);
        filteredCalled.forEach(l => {
            if (l.called_at_iso) {
                const hour = new Date(l.called_at_iso).getHours();
                const idx = Math.floor(hour / 2);
                if (idx >= 0 && idx < 12) {
                    hours[idx]++;
                }
            }
        });

        const maxHr = Math.max(...hours, 1);
        const bars = document.querySelectorAll('.chart-bar');
        bars.forEach((bar, idx) => {
            if (idx < 12) {
                const pct = (hours[idx] / maxHr) * 95;
                bar.style.height = `${pct}%`;
            }
        });

        // Update title with disposition context if active
        const titleEl = document.querySelector('#hourly-distribution h2');
        const disp = window.appState.currentFilter;
        let fullTitle = `Hourly Distribution — ${title}`;
        if (disp && disp !== 'all') fullTitle += ` (${disp})`;
        if (titleEl) titleEl.textContent = fullTitle;

        window.showToast(`Hourly chart: ${title}${disp && disp !== 'all' ? ' · ' + disp : ''}`, 'info');
    };
    window.filterFromKpi = function(kpiKey) {
        const filterMap = {
            'total': 'all',
            'called': 'all',
            'interested': 'Interested',
            'not_interested': 'Not Interested',
            'callbacks': 'Callback',
            'site_visit_scheduled': 'all',
            'whatsapp_sent': 'all',
            'conversion': 'all',
        };
        const filter = filterMap[kpiKey] || 'all';
        window.appState.currentFilter = filter;
        // Update qf-btn visual state
        document.querySelectorAll('.qf-btn').forEach(btn => {
            if (btn.dataset.filter === filter) {
                btn.className = "px-3 py-1 bg-primary text-on-primary rounded-full text-label-sm font-label-sm uppercase";
            } else {
                btn.className = "px-3 py-1 border border-outline-variant text-on-surface-variant rounded-full text-label-sm font-label-sm uppercase hover:bg-surface-container";
            }
        });
        window.renderCallLogsTable();
        // Also update the Engagement Timeline and Hourly Distribution charts
        // so they reflect the active filter (e.g. "Interested" only).
        window.renderFilteredCharts();
    };

    // Engagement chart controls share the dashboard's canonical disposition
    // filter so the graph, hourly distribution, KPIs drill-down and log table
    // never disagree about which leads are being shown.
    window.setEngagementTimelineFilter = function(filter) {
        window.appState.currentFilter = filter || 'all';

        document.querySelectorAll('.qf-btn').forEach(btn => {
            const active = btn.dataset.filter === window.appState.currentFilter;
            btn.className = active
                ? 'qf-btn px-3 py-1 bg-primary text-on-primary rounded-full text-label-sm font-label-sm uppercase'
                : 'qf-btn px-3 py-1 border border-outline-variant text-on-surface-variant rounded-full text-label-sm font-label-sm uppercase hover:bg-surface-container';
        });

        document.querySelectorAll('.engagement-filter-btn').forEach(btn => {
            const active = (window.appState.currentFilter === 'Interested' && btn.id === 'engagement-btn-interested')
                || (window.appState.currentFilter === 'all' && btn.id === 'engagement-btn-all');
            btn.classList.toggle('border-primary', active);
            btn.classList.toggle('text-primary', active);
            btn.classList.toggle('bg-primary-fixed/20', active);
            btn.classList.toggle('border-outline-variant', !active);
            btn.classList.toggle('text-on-surface-variant', !active);
        });

        window.renderCallLogsTable();
        window.renderFilteredCharts();
        if (window.appState.currentFilter === 'all') {
            document.querySelectorAll('.hourly-range-btn').forEach(btn => {
                const active = btn.id === 'hourly-btn-all';
                btn.classList.toggle('border-primary', active);
                btn.classList.toggle('text-primary', active);
                btn.classList.toggle('bg-primary-fixed/20', active);
                btn.classList.toggle('border-outline-variant', !active);
                btn.classList.toggle('text-on-surface-variant', !active);
            });
        }
        window.showToast(
            window.appState.currentFilter === 'Interested'
                ? 'Showing Interested engagement only.'
                : 'Engagement timeline reset to all calls.',
            'info'
        );
    };

    // --- Update charts to reflect active disposition filter ---
    window.renderFilteredCharts = function() {
        const sandboxLeads = window.appState.allLeads.filter(l => l.sandbox === window.appState.currentSandbox);
        let filtered = sandboxLeads;
        const f = window.appState.currentFilter;

        if (f === 'Interested') {
            filtered = filtered.filter(l => l.disposition === 'Interested');
        } else if (f === 'Not Interested') {
            filtered = filtered.filter(l => l.disposition === 'Not Interested');
        } else if (f === 'Callback') {
            filtered = filtered.filter(l => l.disposition === 'Call Later' || l.disposition === 'Callback');
        } else if (f === 'Failed') {
            filtered = filtered.filter(isFailed);
        } else if (f === 'Inbound') {
            filtered = filtered.filter(l => l.source && String(l.source).toLowerCase() === 'inbound');
        } else if (f === 'star4') {
            filtered = filtered.filter(l => {
                if (!l.called_at_iso) return false;
                const d = new Date(l.called_at_iso);
                const now = new Date();
                return (now - d) / (1000 * 60 * 60 * 24) <= 4;
            });
        }
        // else 'all' — use full sandboxLeads

        // Update Engagement Timeline with filtered data
        window._renderEngagementTimeline(filtered.filter(isCalled));
        // Update Hourly Distribution with filtered data
        window._renderHourlyDistribution(filtered.filter(isCalled));
    };

    // --- Engagement Timeline (Area Chart) — filtered data ---
    window._renderEngagementTimeline = function(filteredCalled) {
        const days = ['Sat', 'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri'];
        const callCounts = [0, 0, 0, 0, 0, 0, 0];
        const interestedCounts = [0, 0, 0, 0, 0, 0, 0];

        filteredCalled.forEach(l => {
            if (l.called_at_iso) {
                const date = new Date(l.called_at_iso);
                const day = date.toLocaleDateString([], { weekday: 'short' });
                const idx = days.indexOf(day);
                if (idx !== -1) {
                    callCounts[idx]++;
                    if (l.disposition === 'Interested') {
                        interestedCounts[idx]++;
                    }
                }
            }
        });

        const maxVal = Math.max(...callCounts, 5);
        let pathCalls = "M0,230";
        let pathCallsFill = "M0,230";
        let pathInt = "M0,230";
        let pathIntFill = "M0,230";
        const points = 7;
        const xStep = 800 / (points - 1);

        for (let i = 0; i < points; i++) {
            const x = i * xStep;
            const yCall = 230 - (callCounts[i] / maxVal) * 180;
            const yInt = 230 - (interestedCounts[i] / maxVal) * 180;
            pathCalls += ` L${x},${yCall}`;
            pathCallsFill += ` L${x},${yCall}`;
            pathInt += ` L${x},${yInt}`;
            pathIntFill += ` L${x},${yInt}`;
            if (i === 3) {
                const cMarker = document.getElementById('chart-marker-calls');
                const iMarker = document.getElementById('chart-marker-int');
                if (cMarker) { cMarker.setAttribute('cx', x); cMarker.setAttribute('cy', yCall); }
                if (iMarker) { iMarker.setAttribute('cx', x); iMarker.setAttribute('cy', yInt); }
            }
        }

        window.chartCurrentData = { days, calls: callCounts, interested: interestedCounts, maxVal };

        pathCallsFill += " L800,230 L800,250 L0,250 Z";
        pathIntFill += " L800,230 L800,250 L0,250 Z";

        const svgCallsFill = document.getElementById('svg-calls-fill');
        const svgCallsLine = document.getElementById('svg-calls-line');
        const svgIntFill = document.getElementById('svg-int-fill');
        const svgIntLine = document.getElementById('svg-int-line');
        if (svgCallsFill) svgCallsFill.setAttribute('d', pathCallsFill);
        if (svgCallsLine) svgCallsLine.setAttribute('d', pathCalls);
        if (svgIntFill) svgIntFill.setAttribute('d', pathIntFill);
        if (svgIntLine) svgIntLine.setAttribute('d', pathInt);

        // When Interested is selected, do not draw a duplicate "total calls"
        // series over the interested series. This makes the chart truthful and
        // visually unambiguous for small result sets (for example, 3 leads).
        const interestedOnly = window.appState.currentFilter === 'Interested';
        if (svgCallsFill) svgCallsFill.style.display = interestedOnly ? 'none' : '';
        if (svgCallsLine) svgCallsLine.style.display = interestedOnly ? 'none' : '';

        const yLabels = document.querySelectorAll('#chart-y-axis span');
        if (yLabels.length === 5) {
            yLabels[0].textContent = Math.round(maxVal).toString();
            yLabels[1].textContent = Math.round(maxVal * 0.75).toString();
            yLabels[2].textContent = Math.round(maxVal * 0.5).toString();
            yLabels[3].textContent = Math.round(maxVal * 0.25).toString();
            yLabels[4].textContent = '0';
        }

        // Update hourly distribution title to show active filter
        const titleEl = document.querySelector('#hourly-distribution h2');
        if (titleEl) {
            const f = window.appState.currentFilter;
            if (f === 'all' || !f) titleEl.textContent = 'Hourly Distribution';
            else titleEl.textContent = `Hourly Distribution — ${f}`;
        }
    };

    // --- Hourly Distribution (Bar Chart) — filtered data ---
    window._renderHourlyDistribution = function(filteredCalled) {
        const hours = Array(12).fill(0);
        filteredCalled.forEach(l => {
            if (l.called_at_iso) {
                const hour = new Date(l.called_at_iso).getHours();
                const idx = Math.floor(hour / 2);
                if (idx >= 0 && idx < 12) hours[idx]++;
            }
        });
        window.barCurrentData = { hours };
        const maxHr = Math.max(...hours, 1);
        const bars = document.querySelectorAll('.chart-bar');
        bars.forEach((bar, idx) => {
            if (idx < 12) {
                const pct = (hours[idx] / maxHr) * 95;
                bar.style.height = `${pct}%`;
            }
        });
    };

    // --- Render bottom Call Logs Table ---
     window.renderCallLogsTable = function() {
        const tbody = document.getElementById('call-logs-tbody');
        if (!tbody) return;

        const sandboxLeads = window.appState.allLeads.filter(l => l.sandbox === window.appState.currentSandbox);
        
        // Filter logic
        let filtered = sandboxLeads;

        if (window.appState.currentFilter === 'Interested') {
            filtered = filtered.filter(l => l.disposition === 'Interested');
        } else if (window.appState.currentFilter === 'Not Interested') {
            filtered = filtered.filter(l => l.disposition === 'Not Interested');
        } else if (window.appState.currentFilter === 'Callback') {
            filtered = filtered.filter(l => l.disposition === 'Call Later' || l.disposition === 'Callback');
        } else if (window.appState.currentFilter === 'Failed') {
            filtered = filtered.filter(isFailed);
        } else if (window.appState.currentFilter === 'Inbound') {
            filtered = filtered.filter(l => l.status === 'inbound');
        } else if (window.appState.currentFilter === 'star4') {
            filtered = filtered.filter(l => typeof l.rating === 'number' && l.rating >= 4);
        }

        // Apply Search
        const q = window.appState.tableSearch.toLowerCase().trim();
        if (q) {
            filtered = filtered.filter(l => 
                l.name.toLowerCase().includes(q) || 
                l.phone.toLowerCase().includes(q) || 
                (l.summary && l.summary.toLowerCase().includes(q))
            );
        }

        // Add additional dropdown filters (Location & Budget)
         const filterLoc = document.getElementById('filter-location')?.value || 'All Locations';
         const filterBud = document.getElementById('filter-budget')?.value || 'All Budgets';

         if (filterLoc !== 'All Locations') {
             filtered = filtered.filter(l => l.company && l.company.includes(filterLoc));
         }
         if (filterBud !== 'All Budgets') {
             filtered = filtered.filter(l => {
                 if (!l.summary) return false;
                 if (filterBud === 'Under 1.5Cr') return l.summary.includes('1.5Cr') || l.summary.includes('1.2Cr');
                 if (filterBud === '1.5Cr - 2.0Cr') return l.summary.includes('1.8Cr');
                 return true;
             });
         }

         // Date range filter
         const dateFromVal = document.getElementById('filter-date-from')?.value;
         const dateToVal = document.getElementById('filter-date-to')?.value;
         if (dateFromVal || dateToVal) {
             const fromTs = dateFromVal ? new Date(dateFromVal + 'T00:00:00').getTime() / 1000 : 0;
             const toTs = dateToVal ? new Date(dateToVal + 'T23:59:59').getTime() / 1000 : Infinity;
             filtered = filtered.filter(l => {
                 const ts = l.start_time || l.created_at || 0;
                 return ts >= fromTs && ts <= toTs;
             });
         }

        // Render Table Body
        if (filtered.length === 0) {
            tbody.innerHTML = `<tr><td colspan="9" class="p-8 text-center text-on-surface-variant font-medium">No matching call logs found.</td></tr>`;
            return;
        }

        let html = '';
        filtered.forEach(lead => {
            // Format dates
            const dateStr = lead.called_at_iso 
                ? new Date(lead.called_at_iso).toLocaleDateString([], { day: 'numeric', month: 'short' }) + ", " + 
                  new Date(lead.called_at_iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: true })
                : '—';

            // Rating Stars markup
            let ratingHtml = '—';
            if (typeof lead.rating === 'number') {
                ratingHtml = `<div class="flex text-primary">`;
                for (let i = 1; i <= 5; i++) {
                    const fillClass = i <= lead.rating ? 'fill' : 'opacity-30';
                    ratingHtml += `<span class="material-symbols-outlined text-sm ${fillClass}">star</span>`;
                }
                ratingHtml += `</div>`;
            }

            // Disposition Badge
            let badgeClass = 'bg-surface-container-highest text-on-surface-variant';
            if (lead.disposition === 'Interested') {
                badgeClass = 'bg-tertiary-fixed text-on-tertiary-fixed-variant';
            } else if (lead.disposition === 'Not Interested') {
                badgeClass = 'bg-outline-variant/30 text-outline';
            } else if (lead.disposition === 'Call Later' || lead.disposition === 'Callback') {
                badgeClass = 'bg-secondary-fixed text-on-secondary-fixed-variant';
            } else if (lead.disposition === 'Failed') {
                badgeClass = 'bg-error-container text-error';
            }

            const dispoText = lead.status === 'inbound' ? 'Inbound' : lead.disposition;

            // Inbound row styling
            const rowClass = lead.status === 'inbound' ? 'bg-secondary-fixed/20' : 'hover:bg-surface-container-low transition-colors';

            const listenBtn = lead.duration_sec > 0
                ? `<button class="text-primary hover:underline flex items-center gap-1 font-medium" onclick="event.stopPropagation();window.playAudioRecording('${lead.name}', ${lead.id})">
                     <span class="material-symbols-outlined text-sm">play_circle</span> Listen
                   </button>`
                : '—';
            html += `
            <tr class="border-b border-surface-container ${rowClass} cursor-pointer" onclick="window.openTranscriptModal(${lead.id})">
                <td class="p-4 font-medium flex items-center gap-2">
                    ${lead.name}
                    ${lead.status === 'inbound' ? `<span class="px-1.5 py-0.5 bg-secondary text-on-secondary rounded text-[9px] font-bold uppercase">Inbound</span>` : ''}
                </td>
                <td class="p-4 text-on-surface-variant">${lead.phone}</td>
                <td class="p-4 text-on-surface-variant">${dateStr}</td>
                <td class="p-4 text-on-surface-variant max-w-[320px] truncate" title="${lead.summary}">${lead.summary}</td>
                <td class="p-4">${ratingHtml}</td>
                <td class="p-4"><span class="px-2 py-0.5 ${badgeClass} rounded-full text-[10px] font-bold uppercase">${prettyStatus(dispoText)}</span></td>
                <td class="p-4 text-on-surface-variant font-medium">${lead.error || '—'}</td>
                <td class="p-4">${listenBtn}</td>
                <td class="p-4">
                    <button class="px-3 py-1 border border-outline-variant rounded-md hover:bg-surface-container-high transition-colors text-xs font-semibold" onclick="event.stopPropagation();window.openTranscriptModal(${lead.id})">
                        View
                    </button>
                </td>
            </tr>
            `;
        });

        tbody.innerHTML = html;
    };

    // --- Audio Player Modal Handler ---
    window.playAudioRecording = function(name, leadId) {
        const title = document.getElementById('audio-modal-title');
        const audio = document.getElementById('audio-player-tag');
        if (title) title.textContent = `Call Recording: ${name}`;
        
        if (audio) {
            // Use realistic demo call files
            const index = leadId % 2;
            audio.src = index === 0 
                ? "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
                : "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3";
            audio.load();
        }

        window.openModal('modal-audio-player');
        if (audio) audio.play().catch(() => {});
    };

    window.closeAudioPlayer = function() {
        const audio = document.getElementById('audio-player-tag');
        if (audio) audio.pause();
        window.closeModal('modal-audio-player');
    };

    // --- Transcript & QA Modal Handler ---
    window.openTranscriptModal = function(leadId) {
        const lead = window.appState.allLeads.find(l => l.id === leadId);
        if (!lead) return;

        // Set titles
        document.getElementById('transcript-modal-title').textContent = `Call Detail: ${lead.name}`;
        document.getElementById('transcript-modal-subtitle').textContent = `${lead.phone} • Status: ${prettyStatus(lead.status)}`;

        // Set QA card properties
        document.getElementById('qa-duration').textContent = lead.duration_sec > 0 ? lead.duration_sec + ' seconds' : '—';
        document.getElementById('qa-disposition').textContent = lead.disposition;
        document.getElementById('qa-summary').textContent = lead.summary;

        const recContainer = document.getElementById('transcript-rec-player');
        if (lead.duration_sec > 0) {
            recContainer.style.display = 'block';
            document.getElementById('transcript-audio-player').src = leadId % 2 === 0
                ? "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
                : "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3";
        } else {
            recContainer.style.display = 'none';
        }

        // Populate transcript dialogue
        const container = document.getElementById('transcript-chat-container');
        if (!container) return;

        if (typeof lead.transcript === 'string' && lead.transcript.trim()) {
            const plain = lead.transcript.split('\n').filter(l => l.trim()).map(l => l.trim()).join('\n');
            container.innerHTML = `<pre class="p-4 bg-surface-container-lowest border border-outline-variant rounded text-xs text-on-surface whitespace-pre-wrap break-words m-0 max-h-72 overflow-y-auto">${plain.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</pre>`;
        } else if (Array.isArray(lead.transcript) && lead.transcript.length > 0) {
            let html = '';
            lead.transcript.forEach(t => {
                const isAgent = t.role === 'agent';
                const bubbleClass = isAgent ? 'chat-bubble-agent' : 'chat-bubble-user';
                const sender = isAgent ? 'AI Assistant' : lead.name;
                const alignmentClass = isAgent ? 'items-start' : 'items-end';
                
                html += `
                <div class="flex flex-col ${alignmentClass} gap-1 mb-2">
                    <span class="text-[10px] font-bold text-on-surface-variant uppercase tracking-wider">${sender} • ${t.time}</span>
                    <div class="chat-bubble ${bubbleClass}">${t.text}</div>
                </div>
                `;
            });
            container.innerHTML = html;
        } else {
            container.innerHTML = `<div class="p-8 text-center text-on-surface-variant font-medium">No voice dialogue recorded for this attempt.</div>`;
        }

        window.openModal('modal-call-transcript');
    };

    // --- Expose Global View Switcher ---
    window.switchView = function(viewId) {
        window.appState.currentView = viewId;

        // Hide all views
        document.getElementById('view-dashboard').style.display = 'none';
        document.getElementById('view-campaigns').style.display = 'none';
        document.getElementById('view-make-a-call').style.display = 'none';
        document.getElementById('view-config').style.display = 'none';

        // Show active view
        document.getElementById(`view-${viewId}`).style.display = 'block';

        // Update active navigation link styles
        const links = document.querySelectorAll('.nav-link');
        links.forEach(link => {
            const dest = link.getAttribute('data-view');
            if (dest === viewId) {
                link.classList.remove('text-on-surface-variant', 'hover:bg-surface-container');
                link.classList.add('bg-primary', 'text-on-primary', 'shadow-md', 'scale-[0.98]');
                const icon = link.querySelector('.material-symbols-outlined');
                if (icon) icon.classList.add('fill');
            } else {
                link.classList.add('text-on-surface-variant', 'hover:bg-surface-container');
                link.classList.remove('bg-primary', 'text-on-primary', 'shadow-md', 'scale-[0.98]');
                const icon = link.querySelector('.material-symbols-outlined');
                if (icon) icon.classList.remove('fill');
            }
        });

        if (viewId === 'dashboard') {
            window.updateDashboardUI();
        } else if (viewId === 'campaigns') {
            window.showCampaignList();
            window.loadCampaignContacts();
            window.loadCampaignSourcesList();
            window.loadCampaignConfig();
            window.selectCampaignTab(window.appState.campaignSandbox || 1);
            window.updateCampaignSandboxCounts();
            window.loadCampaignControl();
        } else if (viewId === 'make-a-call') {
            window.loadRecentManualCalls();
        } else if (viewId === 'config') {
            window.loadConfigSettings();
        }
    };

    // ── Campaigns view: Outpero-style full wiring ──────────────────────────────
    window._campHolidays = [];
    window._campRepeatType = 'one_time';

    // --- Sandbox tab switching ---
    window.SELECTED_SANDBOX_INFO = {
        1: { name: 'First-Touch & Callbacks', desc: 'Sandbox 1.1: isolated cold upload on P1/P2 · Sandbox 1.2: automatic digital Excel feed on P3 · callbacks remain here', phones: '1.1 P1/P2 Cold · 1.2 P3 Digital', color: 'primary' },
        2: { name: 'Retry Engine', desc: 'Re-dial failed calls after +12hrs (P4) and +24hrs (P5/P6)', phones: 'P4, P5, P6', color: 'secondary' },
        3: { name: 'Nurture & Blue Loop', desc: 'Interested/site-visit leads: immediate WhatsApp · 24h nudge · P7/P8 call after 2–3h without reply · visit reminders', phones: 'P7, P8', color: 'tertiary' },
        4: { name: 'Post-Visit Feedback', desc: 'Feedback calls day after completed site visit', phones: 'P9', color: 'outline-variant' },
    };
    window.selectCampaignTab = function(sandboxNum) {
        window.appState.campaignSandbox = sandboxNum;
        document.querySelectorAll('.camp-tab').forEach(btn => {
            if (parseInt(btn.dataset.sandbox) === sandboxNum) {
                btn.classList.add('active');
            } else {
                btn.classList.remove('active');
            }
        });
        const info = window.SELECTED_SANDBOX_INFO[sandboxNum];
        const titleEl = document.getElementById('campaign-sb-title');
        const descEl = document.getElementById('campaign-sb-desc');
        const infoEl = document.getElementById('campaign-sandbox-info');
        if (titleEl) titleEl.textContent = sandboxNum;
        if (descEl) descEl.textContent = info.desc;
        if (infoEl) infoEl.textContent = info.phones;

        // Only SB1 allows new campaign uploads; SB2/3/4 get leads from transitions
        const newCampaignBtn = document.querySelector('[onclick="window.showCampaignForm()"]');
        if (newCampaignBtn) {
            newCampaignBtn.style.display = (sandboxNum === 1) ? '' : 'none';
        }

        window.loadCampaigns();
        window.loadCampaignControl();
    };

    // Panel switching
    window.showCampaignForm = function() {
        document.getElementById('campaign-list-panel').style.display = 'none';
        document.getElementById('campaign-form-panel').style.display = 'block';
        window.loadCampaignSourcesList();
        window.loadCampaignConfig();
        // Set sandbox context in form
        const sb = window.appState.campaignSandbox || 1;
        const info = window.SELECTED_SANDBOX_INFO[sb];
        const formTitle = document.getElementById('campaign-form-title');
        if (formTitle) formTitle.textContent = `New Campaign — Sandbox ${sb}: ${info.name}`;
    };
    window.showCampaignList = function() {
        document.getElementById('campaign-list-panel').style.display = 'block';
        document.getElementById('campaign-form-panel').style.display = 'none';
        window.loadCampaigns();
    };

    window.setCampaignControlError = function(message) {
        const el = document.getElementById('campaign-control-error');
        if (!el) return;
        el.textContent = message || '';
        el.classList.toggle('hidden', !message);
    };

    window.setCampaignControlBusy = function(busy, label) {
        ['campaign-control-start','campaign-control-stop','campaign-control-reanalyze','campaign-control-clear'].forEach(id => { const el = document.getElementById(id); if (el) el.disabled = !!busy; });
        const msg = document.getElementById('campaign-control-message');
        if (busy && msg) msg.textContent = label || 'Working…';
    };

    window.loadCampaignControl = async function() {
        const role = encodeURIComponent(window.dashRoleForApi());
        window.setCampaignControlError('');
        try {
            const [stateRes, phoneRes] = await Promise.all([
                fetch(window.apiBase + `/api/campaign/state?role=${role}&_skip_cache=true`, {cache:'no-store'}),
                fetch(window.apiBase + `/api/campaign/phone-numbers?role=${role}`, {cache:'no-store'})
            ]);
            const state = await stateRes.json().catch(() => ({})), phones = await phoneRes.json().catch(() => ({}));
            if (!stateRes.ok) throw new Error(state.detail || 'Campaign state could not be loaded.');
            if (!phoneRes.ok) throw new Error(phones.detail || 'Phone lines could not be loaded.');
            const counts = state.counts || state, total = Number(counts.total || 0), pending = Number(counts.pending || 0), dialing = Number(counts.dialing || 0);
            const pct = total ? Math.min(100, Math.round((Math.max(0, total - pending - dialing) / total) * 100)) : 0;
            const active = !!state.active && !state.campaign_paused;
            document.getElementById('campaign-control-count').textContent = `${total.toLocaleString()} leads`;
            document.getElementById('campaign-control-progress').style.width = `${pct}%`;
            const live = document.getElementById('campaign-control-live'); live.textContent = '● LIVE (WS)'; live.className = 'px-2.5 py-1 rounded-full bg-tertiary-fixed text-on-tertiary-fixed-variant';
            document.getElementById('campaign-control-status').textContent = active ? `Outbound active · ${pending} pending` : `Outbound idle · ${pending} pending`;
            document.getElementById('campaign-control-message').textContent = total === 0 ? 'No leads are loaded. Create a campaign or load a file to begin.' : active ? `Campaign is running. ${dialing} call${dialing === 1 ? '' : 's'} currently dialing.` : 'Campaign is paused — outbound dialing is off. Start it when you are ready.';
            document.getElementById('campaign-control-start').disabled = active || total === 0;
            document.getElementById('campaign-control-stop').disabled = !active;
            const nums = phones.phone_numbers || [];
            document.getElementById('campaign-control-phones').innerHTML = nums.length ? nums.map((n,i) => `<div class="rounded-lg border border-tertiary/30 bg-tertiary/5 px-4 py-3 flex justify-between text-xs"><span><strong>Phone ${i+1}</strong><br><span class="text-on-surface-variant">${n}</span></span><span class="text-tertiary font-semibold">Active</span></div>`).join('') : '<div class="rounded-lg bg-surface-container-low px-4 py-3 text-xs text-on-surface-variant">No outbound phone numbers are configured for this role.</div>';
            document.getElementById('campaign-control-rate').textContent = `Call rate: ${phones.total_calls_this_hour || 0}/${phones.max_calls_per_hour || 0} per hour`;
        } catch (e) { window.setCampaignControlError(e.message || String(e)); const live=document.getElementById('campaign-control-live'); if(live) live.textContent='Service unavailable'; }
    };

    window.campaignControlStart = async function() { window.setCampaignControlBusy(true,'Starting campaign…'); try { await window.quickStartCampaign(); } finally { window.setCampaignControlBusy(false); await window.loadCampaignControl(); } };
    window.campaignControlStop = async function() { window.setCampaignControlBusy(true,'Stopping campaign…'); try { await window.stopCampaign(); } finally { window.setCampaignControlBusy(false); await window.loadCampaignControl(); } };
    window.campaignControlLoadFile = function() { window.showCampaignForm(); setTimeout(() => document.getElementById('camp-file-input')?.click(), 50); };
    window.campaignControlReanalyze = async function() { const role=encodeURIComponent(window.dashRoleForApi()); window.setCampaignControlBusy(true,'Starting analysis…'); window.setCampaignControlError(''); try { const r=await fetch(window.apiBase+`/api/campaign/reanalyze-all?role=${role}`,{method:'POST'}), d=await r.json().catch(()=>({})); if(!r.ok) throw new Error(d.detail||'Re-analysis failed.'); window.showToast(`Re-analysis started for ${d.total||0} calls.`,'success'); } catch(e) { window.setCampaignControlError(e.message||String(e)); } finally { window.setCampaignControlBusy(false); } };
    window.campaignControlClear = async function() { if(!window.confirm('Clear all leads and campaign data for the current role? This cannot be undone.')) return; const role=encodeURIComponent(window.dashRoleForApi()); window.setCampaignControlBusy(true,'Clearing campaign data…'); window.setCampaignControlError(''); try { const r=await fetch(window.apiBase+`/api/campaign/wipe?role=${role}`,{method:'POST'}), d=await r.json().catch(()=>({})); if(!r.ok) throw new Error(d.detail||'Clear failed.'); window.appState.allLeads = []; window.appState.inboundCallbacks = []; window.appState.dataLoadedFromApi = true; window.showToast('All campaign data cleared.','success'); await window.loadRealLeads(); window.updateDashboardUI(); await window.loadCampaigns(); await window.loadCampaignControl(); await window.loadInboundInterest ? window.loadInboundInterest() : null; } catch(e) { window.setCampaignControlError(e.message||String(e)); } finally { window.setCampaignControlBusy(false); } }; 
    window.campaignControlSaveGap = async function() { const input=document.getElementById('campaign-control-gap'), seconds=Number(input?.value), role=encodeURIComponent(window.dashRoleForApi()), btn=document.getElementById('campaign-control-gap-save'); if(!Number.isFinite(seconds)||seconds<0||seconds>1200){window.setCampaignControlError('Pause must be between 0 and 1200 seconds.');return;} if(btn)btn.disabled=true; window.setCampaignControlError(''); try { const r=await fetch(window.apiBase+`/api/campaign/inter-call-gap?role=${role}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({seconds})}), d=await r.json().catch(()=>({})); if(!r.ok) throw new Error(d.detail||'Could not save pause.'); window.showToast('Pause between calls saved.','success'); } catch(e){window.setCampaignControlError(e.message||String(e));} finally{if(btn)btn.disabled=false;} };

    // Load campaign list table (Outpero "All campaigns")
    window.loadCampaigns = async function() {
        const role = encodeURIComponent(window.dashRoleForApi());
        const sandbox = window.appState.campaignSandbox || 1;
        const tbody = document.getElementById('campaign-list-body');
        if (!tbody) return;
        tbody.innerHTML = '<tr><td colspan="7" class="p-3 text-center text-on-surface-variant">Loading campaigns…</td></tr>';
        try {
            const [sourcesRes, stateRes] = await Promise.all([
                fetch(window.apiBase + `/api/campaign/sources?role=${role}&sandbox=${sandbox}`, { cache: 'no-store' }),
                fetch(window.apiBase + `/api/campaign/state?role=${role}`, { cache: 'no-store' }),
            ]);
            const srcData = sourcesRes.ok ? await sourcesRes.json() : { sources: [] };
            const st = stateRes.ok ? await stateRes.json() : {};
            const sources = (srcData.sources) || [];
            const paused = (srcData.paused_sources) || [];
            const sb = window.appState.campaignSandbox || 1;
            if (!sources.length) {
                const emptyMessages = {
                    1: 'No campaigns uploaded yet. Click "+ New outbound campaign" to upload leads.',
                    2: 'No upload sources. Leads here come from failed calls in Sandbox 1 (retry engine).',
                    3: 'No upload sources. Leads here come from interested / site-visit transitions from SB1 & SB2.',
                    4: 'No upload sources. Leads here come from completed site visits for post-visit feedback.'
                };
                tbody.innerHTML = '<tr><td colspan="7" class="p-3 text-center text-on-surface-variant">' + (emptyMessages[sb] || emptyMessages[1]) + '</td></tr>';
                return;
            }
            const stActive = st.active && !st.campaign_paused;
            const stPaused = st.campaign_paused;
            tbody.innerHTML = sources.map(s => {
                const isPaused = paused.includes(s.name);
                const rowStatus = isPaused
                    ? '<span class="px-2 py-0.5 bg-outline-variant/30 text-outline rounded-full font-bold">PAUSED</span>'
                    : (stActive
                        ? '<span class="px-2 py-0.5 bg-tertiary-fixed text-on-tertiary-fixed-variant rounded-full font-bold">ACTIVE</span>'
                        : '<span class="px-2 py-0.5 bg-secondary-fixed text-on-secondary-fixed-variant rounded-full font-bold">READY</span>');
                const toggleBtn = isPaused
                    ? `<button class="text-primary font-semibold hover:underline" onclick="window.toggleCampaignSource('${encodeURIComponent(s.name)}')">Resume</button>`
                    : `<button class="text-primary font-semibold hover:underline" onclick="window.toggleCampaignSource('${encodeURIComponent(s.name)}')">Pause</button>`;
                const delBtn = `<button class="text-error/70 hover:text-error" onclick="window.deleteCampaignSource('${encodeURIComponent(s.name)}')" title="Delete source">
                        <span class="material-symbols-outlined text-base">delete</span>
                    </button>`;
                return `<tr class="border-b border-surface-container hover:bg-surface-container-low">
                    <td class="p-3 font-semibold text-on-surface">${s.name}</td>
                    <td class="p-3 text-on-surface">${sources.length} source${sources.length !== 1 ? 's' : ''}</td>
                    <td class="p-3 text-on-surface">${(s.total || 0).toLocaleString()}</td>
                    <td class="p-3 text-on-surface-variant">${(s.pending || 0).toLocaleString()}</td>
                    <td class="p-3 text-on-surface-variant">${(s.called || 0).toLocaleString()}</td>
                    <td class="p-3">${rowStatus}</td>
                    <td class="p-3 flex gap-2 items-center">${toggleBtn}${delBtn}<button class="text-primary font-semibold hover:underline" onclick="window.startCampaign()">Start</button></td>
                </tr>`;
            }).join('');
        } catch (e) {
            tbody.innerHTML = '<tr><td colspan="7" class="p-3 text-center text-error">Could not load campaign data.</td></tr>';
            window.showToast && window.showToast('Failed to load campaigns: ' + (e.message || e), 'error');
        }
        // Update sandbox tab lead counts
        window.updateCampaignSandboxCounts();
    };

    // Update lead counts on each sandbox tab
    window.updateCampaignSandboxCounts = async function() {
        const role = encodeURIComponent(window.dashRoleForApi());
        for (let i = 1; i <= 4; i++) {
            try {
                const res = await fetch(window.apiBase + `/api/dashboard/leads?sandbox=${i}&limit=1`, { cache: 'no-store' });
                if (res.ok) {
                    const data = await res.json();
                    const el = document.getElementById(`camp-tab-count-${i}`);
                    if (el) el.textContent = (data.count || 0).toLocaleString() + ' leads';
                }
            } catch (_) {}
        }
    };

    window.toggleCampaignSource = async function(name) {
        try {
            const role = encodeURIComponent(window.dashRoleForApi());
            const res = await fetch(window.apiBase + `/api/campaign/sources/toggle?role=${role}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source: decodeURIComponent(name) }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || res.statusText);
            window.showToast('Source ' + decodeURIComponent(name) + ' toggled.', 'success');
            window.loadCampaigns();
        } catch (e) {
            window.showToast('Toggle failed: ' + (e.message || e), 'error');
        }
    };

    window.startCampaign = async function() {
        try {
            const role = encodeURIComponent(window.dashRoleForApi());
            const res = await fetch(window.apiBase + `/api/campaign/start?role=${role}`, { method: 'POST' });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || res.statusText);
            window.showToast('Campaign started.', 'success');
            window.loadCampaigns();
        } catch (e) {
            window.showToast('Start failed: ' + (e.message || e), 'error');
        }
    };

    window.quickStartCampaign = async function() {
         const role = encodeURIComponent(window.dashRoleForApi());
         const btn = document.getElementById('btn-start-campaign');
         if (btn) { btn.disabled = true; btn.textContent = 'Starting…'; }
         try {
             // First merge any pending contacts
             try {
                 await fetch(window.apiBase + `/api/campaign/merge-contacts?role=${role}`, { method: 'POST' });
             } catch (_) {}
             // Then start
             const res = await fetch(window.apiBase + `/api/campaign/start?role=${role}`, { method: 'POST' });
             const data = await res.json().catch(() => ({}));
             if (!res.ok) throw new Error(data.detail || res.statusText);
             window.showToast('Campaign started — dialing will begin within seconds.', 'success');
             window.pollCampaignStatus();
             // Refresh dashboard after a short delay
             setTimeout(async () => {
                 await window.loadRealLeads();
                 window.updateDashboardUI();
                 window.loadCampaigns();
             }, 2000);
         } catch (e) {
             window.showToast('Start failed: ' + (e.message || e), 'error');
         } finally {
             if (btn) { btn.disabled = false; btn.innerHTML = '<span class="material-symbols-outlined text-sm align-middle">play_arrow</span> Start Campaign'; }
         }
     };

    window.stopCampaign = async function() {
        try {
            const role = encodeURIComponent(window.dashRoleForApi());
            const res = await fetch(window.apiBase + `/api/campaign/stop?role=${role}`, { method: 'POST' });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || res.statusText);
            window.showToast('Campaign stopped.', 'success');
            window.loadCampaigns();
        } catch (e) {
            window.showToast('Stop failed: ' + (e.message || e), 'error');
        }
    };

    // Holidays
    window.addHoliday = function() {
        const input = document.getElementById('camp-holiday-date');
        const dateVal = input && input.value;
        if (!dateVal) { window.showToast('Pick a date first.', 'error'); return; }
        if (window._campHolidays.includes(dateVal)) { window.showToast('Date already added.', 'info'); return; }
        window._campHolidays.push(dateVal);
        window.renderHolidays();
        input.value = '';
    };
    window.removeHoliday = function(idx) {
        window._campHolidays.splice(idx, 1);
        window.renderHolidays();
    };
    window.renderHolidays = function() {
        const list = document.getElementById('camp-holidays-list');
        if (!list) return;
        list.innerHTML = window._campHolidays.map((d, i) =>
            `<span class="inline-flex items-center gap-1 px-3 py-1 bg-surface-container-high border border-outline-variant rounded-full text-xs font-semibold text-on-surface">${d} <button onclick="window.removeHoliday(${i})" class="text-on-surface-variant hover:text-error ml-1">✕</button></span>`
        ).join('');
    };

    // Repeat type
    window.setRepeatType = function(type) {
        window._campRepeatType = type;
        document.querySelectorAll('.camp-repeat-btn').forEach(btn => {
            if (btn.dataset.repeat === type) {
                btn.className = 'camp-repeat-btn px-4 py-2 rounded-lg text-xs font-bold border border-primary bg-primary text-on-primary';
            } else {
                btn.className = 'camp-repeat-btn px-4 py-2 rounded-lg text-xs font-bold border border-outline-variant text-on-surface-variant';
            }
        });
    };

    // ── Source type: Cold vs Digital ──
    window._campLeadSource = 'campaign'; // default = Cold Calling
    window.setLeadSource = function(source) {
        window._campLeadSource = source;
        const automation = document.getElementById('digital-sheets-automation');
        if (automation) automation.style.display = source === 'digital' ? 'block' : 'none';
        document.querySelectorAll('.camp-source-btn').forEach(btn => {
            const isActive = btn.dataset.source === source;
            if (isActive) {
                btn.className = 'camp-source-btn flex-1 px-4 py-3 rounded-lg text-sm font-semibold border-2 border-primary bg-primary/10 text-primary transition-all';
            } else {
                btn.className = 'camp-source-btn flex-1 px-4 py-3 rounded-lg text-sm font-semibold border-2 border-outline-variant text-on-surface-variant hover:border-primary/50 transition-all';
            }
        });
        if (source === 'digital') window.loadDigitalSheetsStatus();
    };

    window.loadDigitalSheetsStatus = async function() {
        const status = document.getElementById('digital-sheets-status');
        if (!status) return;
        status.textContent = 'Checking…';
        try {
            const res = await fetch(window.apiBase + '/api/campaign/digital-feed-status', {cache:'no-store'});
            if (!res.ok) throw new Error('Backend unavailable');
            const data = await res.json();
            status.textContent = data.webhook_configured
                ? `${data.connected_brokers}/3 Sheets connected · P3`
                : 'Webhook not configured';
            status.className = data.connected_brokers === 3
                ? 'text-xs font-semibold text-tertiary'
                : 'text-xs font-semibold text-on-surface-variant';
            (data.broker_sheets || []).forEach((broker, index) => {
                const el = document.getElementById(`broker-${index + 1}-status`);
                const card = document.querySelector(`[data-broker-card="${index + 1}"]`);
                if (!el) return;
                el.textContent = broker.url ? 'Connected' : 'Awaiting Sheet URL';
                el.className = broker.url ? 'text-tertiary' : 'text-on-surface-variant';
                window._digitalBrokerSheets[index + 1] = broker.url || '';
                if (card) card.disabled = !broker.url;
            });
        } catch (error) {
            status.textContent = 'Connection unavailable';
            status.className = 'text-xs font-semibold text-error';
        }
    };

    window._digitalBrokerSheets = {};
    window._selectedBrokerSheet = null;
    window.showBrokerSheetPopup = function(brokerNumber) {
        const url = window._digitalBrokerSheets[brokerNumber];
        if (!url) { window.showToast(`Broker ${brokerNumber} Sheet is not connected.`, 'error'); return; }
        window._selectedBrokerSheet = {brokerNumber, url};
        const title = document.getElementById('broker-sheet-popup-title');
        const message = document.getElementById('broker-sheet-popup-message');
        const popup = document.getElementById('broker-sheet-popup');
        if (title) title.textContent = `Broker ${brokerNumber} Digital Leads`;
        if (message) message.textContent = `This opens Broker ${brokerNumber}'s connected Google Sheet in a new tab.`;
        if (popup) popup.style.display = 'flex';
    };
    window.closeBrokerSheetPopup = function() {
        const popup = document.getElementById('broker-sheet-popup');
        if (popup) popup.style.display = 'none';
        window._selectedBrokerSheet = null;
    };
    window.openSelectedBrokerSheet = function() {
        const selected = window._selectedBrokerSheet;
        if (!selected?.url) return;
        window.open(selected.url, '_blank', 'noopener,noreferrer');
        window.closeBrokerSheetPopup();
    };

    window.copyDigitalSheetsSetup = async function() {
        const url = window.location.origin + '/digital-sheets-apps-script.js';
        try {
            const res = await fetch(url);
            if (!res.ok) throw new Error('Setup file unavailable');
            await navigator.clipboard.writeText(await res.text());
            window.showToast('Apps Script copied. Paste it into each broker Sheet.', 'success');
        } catch (error) {
            window.showToast('Could not copy setup: ' + error.message, 'error');
        }
    };

    // ── Contact management ──
    window.showAddContact = function() {
        const panel = document.getElementById('camp-add-contact');
        if (panel) { panel.style.display = 'flex'; document.getElementById('camp-contact-phone').focus(); }
    };
    window.showPasteContacts = function() {
        const panel = document.getElementById('camp-paste-panel');
        if (panel) panel.style.display = 'block';
    };

    window.addSingleContact = async function() {
         const phone = (document.getElementById('camp-contact-phone')?.value || '').trim();
         const name  = (document.getElementById('camp-contact-name')?.value || '').trim();
         if (!phone) { window.showToast('Phone is required.', 'error'); return; }
         const role = encodeURIComponent(window.dashRoleForApi());
         try {
             const res = await fetch(window.apiBase + `/api/campaign/contact?role=${role}`, {
                 method: 'POST',
                 headers: { 'Content-Type': 'application/json' },
                 body: JSON.stringify({ phone, name, source: window._campLeadSource || 'campaign' }),
             });
             if (!res.ok) { const d = await res.json().catch(()=>({})); throw new Error(d.detail || res.statusText); }
             window.showToast('Contact added.', 'success');
             document.getElementById('camp-contact-phone').value = '';
             document.getElementById('camp-contact-name').value = '';
             document.getElementById('camp-add-contact').style.display = 'none';
             window.loadCampaignContacts();
             // Refresh dashboard
             await window.loadRealLeads();
             window.updateDashboardUI();
         } catch (e) {
             window.showToast('Add failed: ' + (e.message || e), 'error');
         }
     };

    window.pasteContacts = async function() {
         const text = (document.getElementById('camp-paste-text')?.value || '').trim();
         if (!text) { window.showToast('Paste some contacts first.', 'error'); return; }
         const role = encodeURIComponent(window.dashRoleForApi());
         try {
             const res = await fetch(window.apiBase + `/api/campaign/contacts/paste?role=${role}`, {
                 method: 'POST',
                 headers: { 'Content-Type': 'application/json' },
                 body: JSON.stringify({ text }),
             });
             if (!res.ok) { const d = await res.json().catch(()=>({})); throw new Error(d.detail || res.statusText); }
             const data = await res.json();
             window.showToast(`Imported ${data.imported || 0} contacts (${data.duplicates || 0} duplicates skipped).`, 'success');
             document.getElementById('camp-paste-panel').style.display = 'none';
             document.getElementById('camp-paste-text').value = '';
             window.loadCampaignContacts();
             // Refresh dashboard
             await window.loadRealLeads();
             window.updateDashboardUI();
         } catch (e) {
             window.showToast('Paste failed: ' + (e.message || e), 'error');
         }
     };

    window.importCampaignCSV = async function(input) {
         const file = input && input.files && input.files[0];
         if (!file) return;
         const role = encodeURIComponent(window.dashRoleForApi());
         const source = window._campLeadSource || 'campaign';
         const sandbox = window.appState.campaignSandbox || 1;
         const fd = new FormData();
         fd.append('file', file);
         const statusEl = document.getElementById('upload-status-msg');
         const sourceLabel = source === 'digital' ? 'Digital' : 'Cold';
         if (statusEl) statusEl.textContent = `Uploading ${file.name} as ${sourceLabel} leads (Sandbox ${sandbox})…`;
         try {
             const res = await fetch(window.apiBase + `/api/campaign/upload?role=${role}&source=${source}&sandbox=${sandbox}`, { method: 'POST', body: fd });
             if (!res.ok) { const d = await res.json().catch(()=>({})); throw new Error(d.detail || res.statusText); }
             const data = await res.json();
             const count = data.count || data.imported || data.added || 0;
             const skipped = data.skipped_duplicates || 0;
             const invalid = data.cleaning ? data.cleaning.invalid_phones : 0;
             if (statusEl) {
                 statusEl.textContent = `✓ Saved ${count} ${sourceLabel} leads` + (skipped ? `, ${skipped} duplicates skipped` : '') + (invalid ? `, ${invalid} invalid numbers` : '') + '.';
                 statusEl.className = 'text-xs text-tertiary mt-3 font-semibold';
             }
             window.showToast(`Uploaded ${count} ${sourceLabel} leads from ${file.name}.`, 'success');
             window.loadCampaignContacts();
             window.loadCampaignSourcesList();
             // CRITICAL: Refresh dashboard data so uploaded leads appear immediately
             await window.loadRealLeads();
             window.updateDashboardUI();
         } catch (e) {
             if (statusEl) {
                 statusEl.textContent = '✗ Upload failed: ' + (e.message || e);
                 statusEl.className = 'text-xs text-error mt-3 font-semibold';
             }
             window.showToast('Upload failed: ' + (e.message || e), 'error');
         }
         input.value = '';
     };

    window.loadCampaignContacts = async function() {
         const role = encodeURIComponent(window.dashRoleForApi());
         const tbody = document.getElementById('camp-contacts-body');
         const countEl = document.getElementById('camp-contact-count');
         if (!tbody) return;
         tbody.innerHTML = '<tr><td colspan="4" class="p-3 text-center text-on-surface-variant">Loading…</td></tr>';
         try {
             // Fetch both campaign_contacts AND leads with status
             const [contactsRes, leadsRes] = await Promise.all([
                 fetch(window.apiBase + `/api/campaign/contacts?role=${role}&source=${encodeURIComponent(window._campLeadSource || 'campaign')}`, { cache: 'no-store' }),
                 fetch(window.apiBase + `/api/dashboard/leads?limit=1000&role=${role}&lead_source=${encodeURIComponent(window._campLeadSource || 'campaign')}`, { cache: 'no-store' }),
             ]);
             const contactsData = contactsRes.ok ? await contactsRes.json() : { contacts: [] };
             const leadsData = leadsRes.ok ? await leadsRes.json() : { leads: [] };
             const contacts = (contactsData.contacts) || [];
             const wantedSource = window._campLeadSource || 'campaign';
             const leads = (leadsData.leads) || [];
             // Build phone→lead status map
             const phoneStatusMap = {};
             leads.forEach(l => {
                 if (l.phone) phoneStatusMap[l.phone] = l;
             });
             if (countEl) {
                 const withStatus = contacts.filter(c => phoneStatusMap[c.phone]).length;
                 countEl.textContent = contacts.length + ' contacts — ' + withStatus + ' with call status. Upload CSV to add more.';
             }
             if (!contacts.length && !leads.length) {
                 tbody.innerHTML = '<tr><td colspan="4" class="p-3 text-center text-on-surface-variant">No contacts yet — Add / Import / Paste above.</td></tr>';
                 return;
             }
             // Merge: show contacts with their lead status if available
             const allPhones = [...new Set([...contacts.map(c => c.phone), ...leads.map(l => l.phone)])];
             const items = allPhones.map(phone => {
                 const contact = contacts.find(c => c.phone === phone);
                 const lead = phoneStatusMap[phone];
                 return { phone, name: contact?.name || lead?.name || '—', lead, contact };
             });
             tbody.innerHTML = items.map((item, i) => {
                 const lead = item.lead;
                 let statusBadge = '<span class="px-2 py-0.5 bg-surface-container-highest text-on-surface-variant rounded-full text-[9px] font-bold">PENDING</span>';
                 if (lead) {
                     const dispo = (lead.disposition || '').toLowerCase();
                     const status = (lead.status || '').toLowerCase();
                     if (dispo === 'interested' || status === 'interested') {
                         statusBadge = '<span class="px-2 py-0.5 bg-tertiary-fixed text-on-tertiary-fixed-variant rounded-full text-[9px] font-bold">INTERESTED</span>';
                     } else if (dispo === 'not interested' || status === 'not_interested') {
                         statusBadge = '<span class="px-2 py-0.5 bg-outline-variant/40 text-on-surface-variant rounded-full text-[9px] font-bold">NOT INTERESTED</span>';
                     } else if (dispo === 'call later' || dispo === 'callback' || status === 'callback_scheduled') {
                         statusBadge = '<span class="px-2 py-0.5 bg-secondary-fixed text-on-secondary-fixed-variant rounded-full text-[9px] font-bold">CALLBACK</span>';
                     } else if (status === 'site_visit' || lead.site_visit_scheduled) {
                         statusBadge = '<span class="px-2 py-0.5 bg-primary/20 text-primary rounded-full text-[9px] font-bold">SITE VISIT</span>';
                     } else if (status === 'completed' || status === 'answered') {
                         statusBadge = '<span class="px-2 py-0.5 bg-tertiary-fixed/50 text-on-tertiary-fixed-variant rounded-full text-[9px] font-bold">ANSWERED</span>';
                     } else if (status === 'failed') {
                         statusBadge = '<span class="px-2 py-0.5 bg-error-container text-on-error-container rounded-full text-[9px] font-bold">NO ANSWER</span>';
                     } else if (status === 'dialing') {
                         statusBadge = '<span class="px-2 py-0.5 bg-primary/20 text-primary rounded-full text-[9px] font-bold">DIALING</span>';
                     }
                 }
                 const callDate = lead?.called_at_iso ? new Date(lead.called_at_iso).toLocaleDateString([], {day:'numeric',month:'short'}) + ' ' + new Date(lead.called_at_iso).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '—';
                 return `<tr class="border-b border-surface-container hover:bg-surface-container-low">
                     <td class="p-2 text-on-surface font-medium">${item.phone || ''}</td>
                     <td class="p-2 text-on-surface-variant">${item.name || '—'}</td>
                     <td class="p-2">${statusBadge}</td>
                     <td class="p-2 text-on-surface-variant text-[10px]">${callDate}</td>
                 </tr>`;
             }).join('');
         } catch (e) {
             tbody.innerHTML = '<tr><td colspan="4" class="p-3 text-center text-error">Failed to load contacts.</td></tr>';
         }
     };

    window.removeCampaignContact = async function(contactId) {
        const role = encodeURIComponent(window.dashRoleForApi());
        try {
            await fetch(window.apiBase + `/api/campaign/contacts/${contactId}?role=${role}`, { method: 'DELETE' });
            window.loadCampaignContacts();
        } catch (_) {}
    };

    // ── Source management: list & delete ──
    window.loadCampaignSourcesList = async function() {
        const role = encodeURIComponent(window.dashRoleForApi());
        const container = document.getElementById('camp-sources-list');
        if (!container) return;
        try {
            const res = await fetch(window.apiBase + `/api/campaign/sources?role=${role}`, { cache: 'no-store' });
            if (!res.ok) throw new Error('failed');
            const data = await res.json();
            const sources = data.sources || [];
            if (!sources.length) {
                container.innerHTML = '<span class="text-xs text-on-surface-variant">No sources loaded. Upload a file above.</span>';
                return;
            }
            container.innerHTML = sources.map(s => `
                <div class="flex items-center gap-2 bg-surface-container px-3 py-1.5 rounded-full border border-surface-container">
                    <span class="text-xs font-medium text-on-surface">${s.name}</span>
                    <span class="text-[10px] text-on-surface-variant">(${s.total || 0})</span>
                    <button type="button" class="text-error hover:text-error/70" onclick="window.deleteCampaignSource('${encodeURIComponent(s.name)}')" title="Remove this source">
                        <span class="material-symbols-outlined text-sm">close</span>
                    </button>
                </div>
            `).join('');
        } catch (_) {
            container.innerHTML = '<span class="text-xs text-error">Could not load sources.</span>';
        }
    };

    window.deleteCampaignSource = async function(encodedName) {
        const name = decodeURIComponent(encodedName);
        if (!confirm(`Remove all leads from "${name}"? This cannot be undone.`)) return;
        const role = encodeURIComponent(window.dashRoleForApi());
        try {
            const res = await fetch(window.apiBase + `/api/campaign/sources?source=${encodeURIComponent(name)}&role=${role}`, { method: 'DELETE' });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || 'Delete failed');
            window.showToast(`Removed source "${name}" (${data.deleted || 0} leads).`, 'success');
            window.loadCampaignSourcesList();
            window.loadCampaignContacts();
            await window.loadRealLeads();
            window.updateDashboardUI();
        } catch (e) {
            window.showToast('Delete failed: ' + (e.message || e), 'error');
        }
    };

    window.deleteAllSources = async function() {
        const role = encodeURIComponent(window.dashRoleForApi());
        if (!confirm('Remove ALL uploaded sources and leads? This cannot be undone.')) return;
        try {
            const res = await fetch(window.apiBase + `/api/campaign/sources?role=${role}`, { cache: 'no-store' });
            const data = await res.json().catch(() => ({ sources: [] }));
            const sources = data.sources || [];
            let totalDeleted = 0;
            for (const s of sources) {
                const delRes = await fetch(window.apiBase + `/api/campaign/sources?source=${encodeURIComponent(s.name)}&role=${role}`, { method: 'DELETE' });
                const delData = await delRes.json().catch(() => ({}));
                totalDeleted += delData.deleted || 0;
            }
            const clearRes = await fetch(window.apiBase + `/api/campaign/contacts?role=${role}&source=${encodeURIComponent(window._campLeadSource || 'campaign')}`, { method: 'DELETE' });
            const clearData = await clearRes.json().catch(() => ({}));
            if (!clearRes.ok) throw new Error(clearData.detail || 'Contact cleanup failed');
            const statusEl = document.getElementById('upload-status-msg');
            if (statusEl) { statusEl.textContent = ''; statusEl.className = 'text-xs text-on-surface-variant mt-3'; }
            window.showToast(`Removed ${sources.length} source(s), ${totalDeleted} leads and ${clearData.deleted || 0} contacts.`, 'success');
            window.loadCampaignSourcesList();
            window.loadCampaignContacts();
            await window.loadRealLeads();
            window.updateDashboardUI();
        } catch (e) {
            window.showToast('Delete all failed: ' + (e.message || e), 'error');
        }
    };

    // ── Launch campaign ──
    window.launchCampaign = async function() {
        const role = encodeURIComponent(window.dashRoleForApi());
        const name = (document.getElementById('camp-name')?.value || '').trim();
        if (!name) { window.showToast('Campaign name is required.', 'error'); return; }

        // Gather days
        const days = [];
        document.querySelectorAll('#camp-days .camp-day-btn').forEach(btn => {
            if (btn.classList.contains('bg-primary') && btn.classList.contains('text-on-primary')) {
                days.push(parseInt(btn.dataset.day));
            }
        });

        // Gather schedule
        const scheduleEnabled = document.getElementById('camp-schedule-toggle')?.checked;
        const scheduleTime = scheduleEnabled ? document.getElementById('camp-schedule-datetime')?.value : null;

        const config = {
            campaign_name: name,
            concurrent_call_limit: parseInt(document.getElementById('camp-concurrent')?.value) || 2,
            window_start: document.getElementById('camp-window-start')?.value || null,
            window_end: document.getElementById('camp-window-end')?.value || null,
            skip_opted_out: document.getElementById('camp-skip-opted')?.checked ?? true,
            calling_days: days,
            holidays: window._campHolidays,
            skip_recently_days: parseInt(document.getElementById('camp-skip-days')?.value) || 0,
            retry_count: parseInt(document.getElementById('camp-retry-count')?.value) || 2,
            retry_when: document.getElementById('camp-retry-when')?.value || 'next_day',
            repeat_type: window._campRepeatType,
            schedule_at: scheduleTime,
            sandbox: window.appState.campaignSandbox || 1,
            lead_source: window._campLeadSource || 'campaign',
        };

        const btn = document.getElementById('camp-launch-btn');
        if (btn) { btn.disabled = true; btn.textContent = 'Launching…'; }

        try {
            // 1. Save config
            const cfgRes = await fetch(window.apiBase + `/api/campaign/config?role=${role}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config),
            });
            if (!cfgRes.ok) { const d = await cfgRes.json().catch(()=>({})); throw new Error(d.detail || cfgRes.statusText); }

            // 2. Merge campaign_contacts into leads
            try {
                const mergeRes = await fetch(window.apiBase + `/api/campaign/merge-contacts?role=${role}`, { method: 'POST' });
                if (mergeRes.ok) {
                    const mergeData = await mergeRes.json();
                    if (mergeData.merged > 0) {
                        window.showToast(`Merged ${mergeData.merged} contacts into leads.`, 'info');
                    }
                }
            } catch (_) { /* merge is best-effort */ }

            // 3. Start campaign
            const startRes = await fetch(window.apiBase + `/api/campaign/start?role=${role}`, { method: 'POST' });
            if (!startRes.ok) { const d = await startRes.json().catch(()=>({})); throw new Error(d.detail || startRes.statusText); }

            window.showToast('Campaign "' + name + '" launched successfully!', 'success');
            window.showCampaignList();
        } catch (e) {
            window.showToast('Launch failed: ' + (e.message || e), 'error');
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = 'Launch Outbound Campaign'; }
        }
    };

    // Load config on form open
    window.loadCampaignConfig = async function() {
        const role = encodeURIComponent(window.dashRoleForApi());
        try {
            const res = await fetch(window.apiBase + `/api/campaign/config?role=${role}`, { cache: 'no-store' });
            if (!res.ok) return;
            const response = await res.json();
            const cfg = response.config || response;
            const set = (id, v) => { const el = document.getElementById(id); if (el && v != null) el.value = String(v); };
            const setCheck = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };
            // Support both field name formats (window_start / calling_window_start)
            const winStart = cfg.window_start || cfg.calling_window_start || '11:00';
            const winEnd = cfg.window_end || cfg.calling_window_end || '19:30';
            set('camp-name', cfg.campaign_name);
            set('camp-concurrent', cfg.concurrent_call_limit);
            set('camp-window-start', winStart);
            set('camp-window-end', winEnd);
            setCheck('camp-skip-opted', cfg.skip_opted_out !== false);
            set('camp-skip-days', cfg.skip_recently_days || cfg.skip_recently_called_days || 0);
            set('camp-retry-count', cfg.retry_count || cfg.auto_retry_count || 2);
            set('camp-retry-when', cfg.retry_when || cfg.auto_retry_when || 'next_day');
            if (cfg.calling_days && cfg.calling_days.length) {
                document.querySelectorAll('#camp-days .camp-day-btn').forEach(btn => {
                    const d = parseInt(btn.dataset.day);
                    const active = cfg.calling_days.includes(d);
                    btn.classList.toggle('bg-primary', active);
                    btn.classList.toggle('text-on-primary', active);
                    btn.classList.toggle('bg-surface', !active);
                    btn.classList.toggle('text-on-surface-variant', !active);
                    btn.classList.toggle('border-outline-variant', !active);
                    btn.classList.toggle('border-primary', active);
                });
            }
            if (cfg.holidays) {
                window._campHolidays = cfg.holidays;
                window.renderHolidays();
            }
            if (cfg.repeat_type) window.setRepeatType(cfg.repeat_type);
            window.setLeadSource(cfg.lead_source || 'campaign');
            if (cfg.schedule_at) {
                const tog = document.getElementById('camp-schedule-toggle');
                if (tog) { tog.checked = true; document.getElementById('camp-schedule-fields').style.display = 'block'; }
                set('camp-schedule-datetime', cfg.schedule_at);
            }
            const windowStatus = document.getElementById('camp-window-status');
            if (windowStatus) windowStatus.textContent = `Saved: ${winStart}–${winEnd} IST`;
        } catch (e) {
            const windowStatus = document.getElementById('camp-window-status');
            if (windowStatus) { windowStatus.textContent = 'Could not load saved calling window.'; windowStatus.className = 'text-xs text-error'; }
        }
    };

    window.saveCampaignWindow = async function() {
        const start = document.getElementById('camp-window-start')?.value || '11:00';
        const end = document.getElementById('camp-window-end')?.value || '19:30';
        const status = document.getElementById('camp-window-status');
        const btn = document.getElementById('camp-window-save');
        if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(start) || !/^([01]\d|2[0-3]):[0-5]\d$/.test(end) || start >= end) {
            if (status) { status.textContent = 'Choose a valid start time earlier than the end time.'; status.className = 'text-xs text-error'; }
            return;
        }
        const role = encodeURIComponent(window.dashRoleForApi());
        if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
        if (status) { status.textContent = 'Saving…'; status.className = 'text-xs text-on-surface-variant'; }
        try {
            const currentRes = await fetch(window.apiBase + `/api/campaign/config?role=${role}`, {cache:'no-store'});
            const currentData = await currentRes.json().catch(() => ({}));
            if (!currentRes.ok) throw new Error(currentData.detail || 'Could not load campaign settings.');
            const config = {...(currentData.config || currentData), window_start:start, window_end:end, calling_window_start:start, calling_window_end:end};
            const saveRes = await fetch(window.apiBase + `/api/campaign/config?role=${role}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(config)});
            const saved = await saveRes.json().catch(() => ({}));
            if (!saveRes.ok) throw new Error(saved.detail || 'Could not save calling window.');
            if (status) { status.textContent = `Saved: ${start}–${end} IST`; status.className = 'text-xs text-tertiary font-semibold'; }
            window.showToast('Calling window saved.', 'success');
        } catch (e) {
            if (status) { status.textContent = e.message || 'Could not save calling window.'; status.className = 'text-xs text-error'; }
        } finally { if (btn) { btn.disabled = false; btn.textContent = 'Save calling window'; } }
    };

    window.downloadLeadsCSV = function() {
         const role = encodeURIComponent(window.dashRoleForApi());
         const filter = window.appState.currentFilter || 'all';
         window.open(window.apiBase + `/api/campaign/download?role=${role}&filter=${filter}`, '_blank');
     };

    window.uploadCampaignFile = async function(input) {
        const file = input && input.files && input.files[0];
        if (!file) return;
        try {
            const role = encodeURIComponent(window.dashRoleForApi());
            const source = window._campLeadSource || 'campaign';
            const sandbox = window.appState.campaignSandbox || 1;
            const fd = new FormData();
            fd.append('file', file);
            const res = await fetch(window.apiBase + `/api/campaign/upload?role=${role}&source=${source}&sandbox=${sandbox}`, { method: 'POST', body: fd });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || res.statusText);
            window.showToast('Uploaded ' + file.name + ' — ' + ((data.imported || data.added || data.count) || ''), 'success');
            input.value = '';
            window.loadCampaigns();
        } catch (e) {
            window.showToast('Upload failed: ' + (e.message || e), 'error');
        }
    };

    // ── Make a Call view: real manual call via Vobiz ────────────────────────
    window.placeRealCall = async function() {
        if (!window.dashEnsureAuth()) return;
        const dialNumber = (document.getElementById('dial-phone-input')?.value || '').trim();
        if (!dialNumber || dialNumber.length < 10) {
            window.showToast("Please enter a valid phone number", "error");
            return;
        }
        const name = (document.getElementById('dial-name-input')?.value || '').trim();
        const dialBtn = document.getElementById('dial-action-btn');
        const activeContainer = document.getElementById('active-call-panel');
        const resultBody = document.getElementById('call-result-body');
        const statusTitle = document.getElementById('manual-call-status-title');
        const statusDot = document.getElementById('manual-call-status-dot');

        dialBtn.disabled = true;
        dialBtn.textContent = 'Calling...';
        activeContainer.style.display = 'block';
        if (statusTitle) statusTitle.textContent = 'CALL IN PROGRESS';
        if (statusDot) { statusDot.classList.add('pulse-active'); statusDot.classList.remove('bg-error'); statusDot.classList.add('bg-primary'); }
        if (resultBody) resultBody.innerHTML = 'Dialing ' + dialNumber + ' via Vobiz…<br><span style="font-size:11px;">Outcome appears when the call ends (30–90s after hangup).</span>';

        const abort = new AbortController();
        const abortTimer = setTimeout(() => abort.abort(), 45000);
        try {
            const role = encodeURIComponent(window.dashRoleForApi());
            const res = await fetch(window.apiBase + `/api/manual/call?role=${role}`, {
                method: 'POST',
                headers: window.dashAuthHeaders(),
                signal: abort.signal,
                body: JSON.stringify({ to: dialNumber, callee_name: name }),
            });
            let data = {};
            try { data = await res.json(); } catch (_) {}
            if (!res.ok) {
                if (res.status === 401) {
                    window.showToast('Session expired — signing you in again.', 'error');
                    setTimeout(() => { window.location.href = '/login'; }, 900);
                    return;
                }
                throw new Error(typeof data.detail === 'string' ? data.detail : (data.detail || res.statusText));
            }
            if (resultBody) resultBody.innerHTML = '<span style="color:var(--success);font-weight:700;">Call initiated</span> — dialing ' + dialNumber + '…<br><span style="font-size:11px;">' + (data.message || '') + '</span>';
             const mid = data.manual_call_id;
             window.showToast('Call initiated. Analyzing transcript after hangup…', 'success');
             window.loadRecentManualCalls();
             if (mid) {
                 window.pollManualCallComplete(mid).then(row => {
                     // Show disposition panel for manual marking
                     document.getElementById('call-disposition-panel').style.display = 'block';
                     if (row && row.lead_id) {
                         window._currentManualCallLeadId = row.lead_id;
                     } else if (row && row.id) {
                         window._currentManualCallLeadId = row.id;
                     }
                     if (resultBody) {
                         if (!row) {
                             resultBody.innerHTML = 'Call ended — outcome will appear below.';
                             return;
                         }
                         const dispo = row.disposition || row.status || 'completed';
                         const sum = (row.summary || '').slice(0, 220);
                         resultBody.innerHTML = '<span style="color:var(--success);font-weight:700;">Call ended — ' + dispo + '</span><br><span style="font-size:11px;">' + sum + '</span>';
                     }
                     window.loadRecentManualCalls();
                 });
             }
        } catch (e) {
            const msg = (e && e.name === 'AbortError')
                ? 'Call request timed out (45s). Server may be busy — try again.'
                : ((e && e.message) ? e.message : String(e));
            if (resultBody) resultBody.innerHTML = '<span style="color:var(--error);font-weight:700;">Error:</span> ' + msg;
            if (statusTitle) statusTitle.textContent = 'CALL NOT STARTED';
            if (statusDot) { statusDot.classList.remove('pulse-active','bg-primary'); statusDot.classList.add('bg-error'); }
            window.showToast('Error: ' + msg, 'error');
        } finally {
            clearTimeout(abortTimer);
            dialBtn.disabled = false;
            dialBtn.textContent = 'Dial';
        }
    };

    // ── Call Disposition ──
     window._currentManualCallLeadId = null;
     window.setCallDisposition = async function(dispo) {
         const statusEl = document.getElementById('dispo-status');
         const leadId = window._currentManualCallLeadId;
         if (!leadId) {
             if (statusEl) statusEl.textContent = 'No active call to set disposition for.';
             return;
         }
         const role = encodeURIComponent(window.dashRoleForApi());
         const statusMap = {
             'interested': 'interested',
             'callback': 'callback_scheduled',
             'not_interested': 'not_interested',
             'site_visit': 'site_visit',
         };
         const newStatus = statusMap[dispo] || 'completed';
         try {
             const res = await fetch(window.apiBase + `/api/campaign/lead/${leadId}/status?role=${role}`, {
                 method: 'POST',
                 headers: { 'Content-Type': 'application/json' },
                 body: JSON.stringify({ status: newStatus }),
             });
             if (!res.ok) { const d = await res.json().catch(()=>({})); throw new Error(d.detail || res.statusText); }
             if (statusEl) statusEl.textContent = `✓ Lead marked as ${dispo.replace('_', ' ')}.`;
             // Highlight selected button
             document.querySelectorAll('.dispo-btn').forEach(btn => {
                 btn.classList.remove('ring-2', 'ring-primary', 'bg-primary/10');
                 if (btn.dataset.dispo === dispo) btn.classList.add('ring-2', 'ring-primary', 'bg-primary/10');
             });
             // Refresh dashboard and recent calls
             await window.loadRealLeads();
             window.updateDashboardUI();
             window.loadRecentManualCalls();
         } catch (e) {
             if (statusEl) statusEl.textContent = 'Failed: ' + (e.message || e);
             window.showToast('Disposition update failed: ' + (e.message || e), 'error');
         }
     };

    window.pollManualCallComplete = async function(callId, tries) {
        tries = tries || 0;
        if (tries > 24) return null;
        try {
            const role = encodeURIComponent(window.dashRoleForApi());
            const res = await fetch(window.apiBase + `/api/manual/calls/${callId}?role=${role}`, { headers: window.dashAuthHeaders(), cache: 'no-store' });
            if (!res.ok) return null;
            const row = await res.json();
            if (row.status === 'completed' || row.status === 'failed') return row;
        } catch (_) {}
        await new Promise(r => setTimeout(r, 5000));
        return window.pollManualCallComplete(callId, tries + 1);
    };

    window.loadRecentManualCalls = async function() {
        if (!window.dashToken()) return;
        const bodyEl = document.getElementById('recent-manual-calls-body');
        try {
            const role = encodeURIComponent(window.dashRoleForApi());
            const res = await fetch(window.apiBase + `/api/manual/calls/recent?role=${role}&limit=10`, { headers: window.dashAuthHeaders(), cache: 'no-store' });
            if (!res.ok) throw new Error(res.statusText);
            const data = await res.json();
            const items = (data.items) || [];
            if (!items.length) {
                if (bodyEl) bodyEl.innerHTML = '<span class="text-xs text-on-surface-variant">No manual calls logged yet for this role.</span>';
                return;
            }
             if (bodyEl) bodyEl.innerHTML = items.map(r => {
                 const st = r.status || '';
                 const dispo = r.disposition || '';
                 const phone = r.to_phone || '';
                 const rawWhen = r.started_at || r.created_at || r.updated_at || '';
                 let when = 'Time unavailable';
                 if (rawWhen) {
                     const parsed = typeof rawWhen === 'number' ? new Date(rawWhen * 1000) : new Date(rawWhen);
                     if (!Number.isNaN(parsed.getTime())) when = parsed.toLocaleString();
                 }
                 let badge = '';
                 const d = dispo.toLowerCase();
                 if (d === 'interested' || st === 'interested') {
                     badge = '<span class="px-2 py-1 bg-tertiary-fixed text-on-tertiary-fixed-variant rounded-full font-bold text-[10px]">INTERESTED</span>';
                 } else if (d === 'callback' || d === 'call later' || st === 'callback_scheduled') {
                     badge = '<span class="px-2 py-1 bg-secondary-fixed text-on-secondary-fixed-variant rounded-full font-bold text-[10px]">CALLBACK</span>';
                 } else if (d === 'not interested' || st === 'not_interested') {
                     badge = '<span class="px-2 py-1 bg-outline-variant/40 text-on-surface-variant rounded-full font-bold text-[10px]">NOT INTERESTED</span>';
                 } else if (d === 'site_visit' || d === 'site visit' || st === 'site_visit') {
                     badge = '<span class="px-2 py-1 bg-primary/20 text-primary rounded-full font-bold text-[10px]">SITE VISIT</span>';
                 } else if (st === 'completed' || st === 'answered') {
                     badge = '<span class="px-2 py-1 bg-tertiary-fixed/50 text-on-tertiary-fixed-variant rounded-full font-bold text-[10px]">DONE</span>';
                 } else if (st === 'failed') {
                     badge = '<span class="px-2 py-1 bg-error-container text-on-error-container rounded-full font-bold text-[10px]">FAILED</span>';
                 } else {
                     badge = '<span class="px-2 py-1 bg-secondary-fixed text-on-secondary-fixed-variant rounded-full font-bold text-[10px]">' + (st || 'UNKNOWN').toUpperCase() + '</span>';
                 }
                 return `<button type="button" onclick="window.openManualCallDetail(${Number(r.id)})" class="w-full text-left flex items-center justify-between border border-outline-variant rounded-lg p-3 bg-surface-container-low hover:border-primary hover:bg-primary/5 transition-colors" aria-label="Open call details for ${phone}">
                     <div class="flex-1">
                         <div class="flex items-center gap-2">
                             <span class="font-semibold text-on-surface text-sm">${r.callee_name || '—'}</span>
                             <span class="text-on-surface-variant text-xs">${phone}</span>
                         </div>
                         <div class="text-[11px] text-on-surface-variant mt-1 line-clamp-1">${(r.summary || '').slice(0, 120) || 'No summary yet'}</div>
                         <div class="text-[10px] text-on-surface-variant mt-0.5">${when}</div>
                     </div>
                     <div class="ml-3">${badge}</div>
                 </button>`;
             }).join('');
        } catch (e) {
            if (bodyEl) bodyEl.innerHTML = '<span class="text-xs text-error">Could not load recent manual calls.</span>';
        }
    };

    window.closeManualCallDetail = function() {
        const modal = document.getElementById('manual-call-detail-modal');
        const audio = document.getElementById('manual-detail-audio');
        if (audio) { audio.pause(); audio.removeAttribute('src'); }
        if (modal) modal.style.display = 'none';
    };

    window.openManualCallDetail = async function(callId) {
        const modal = document.getElementById('manual-call-detail-modal');
        const title = document.getElementById('manual-detail-title');
        const sub = document.getElementById('manual-detail-sub');
        const summary = document.getElementById('manual-detail-summary');
        const transcript = document.getElementById('manual-detail-transcript');
        const audio = document.getElementById('manual-detail-audio');
        const recStatus = document.getElementById('manual-detail-recording-status');
        if (!modal) return;
        modal.style.display = 'flex';
        title.textContent = 'Loading call details…';
        sub.textContent = summary.textContent = transcript.textContent = '';
        recStatus.textContent = 'Checking recording…';
        audio.style.display = 'none';
        try {
            const role = encodeURIComponent(window.dashRoleForApi());
            const res = await fetch(window.apiBase + `/api/manual/calls/${callId}?role=${role}`, { headers: window.dashAuthHeaders(), cache: 'no-store' });
            const row = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(row.detail || res.statusText);
            title.textContent = row.callee_name || row.to_phone || 'Manual call';
            sub.textContent = `${row.to_phone || ''} · ${row.status || ''} · ${row.disposition || ''}`;
            summary.textContent = row.summary || row.analysis_summary || 'Outcome analysis is still processing.';
            transcript.textContent = row.transcript || row.transcript_text || 'Transcript is still processing.';
            if (row.recording_available || row.recording_pending || row.log_id) {
                const token = encodeURIComponent(window.dashToken() || '');
                audio.src = window.apiBase + `/api/manual/calls/${callId}/recording?role=${role}&access_token=${token}`;
                audio.style.display = 'block';
                recStatus.textContent = row.recording_available ? 'Recording ready.' : 'Recording is finalizing; press play again shortly.';
            } else {
                recStatus.textContent = 'No carrier recording has been attached to this call yet.';
            }
        } catch (e) {
            title.textContent = 'Could not open call details';
            summary.textContent = e.message || String(e);
            recStatus.textContent = '';
        }
    };

    // ── Configuration view: real /api/tuning wiring ─────────────────────────
    window.loadConfigSettings = async function() {
        const role = encodeURIComponent(window.dashRoleForApi());
        const set = (id, v) => {
            const el = document.getElementById(id);
            if (el) el.value = (v != null) ? String(v) : '';
        };
        // Set role label
        const roleLabel = document.getElementById('config-role-label');
        if (roleLabel) roleLabel.textContent = window.dashRoleForApi();
        try {
            const res = await fetch(window.apiBase + `/api/tuning?role=${role}`, { cache: 'no-store' });
            if (!res.ok) throw new Error(res.statusText);
            const d = await res.json();
            set('config-prompt', d.prompt);
            set('config-rag', d.rag);
            set('config-greeting', d.greeting_text);
        } catch (e) {
            window.showToast && window.showToast('Failed to load config: ' + (e.message || e), 'error');
        }
        // Load campaign cases
        window.loadCases();
    };

    window.saveConfigSettings = async function() {
        const role = encodeURIComponent(window.dashRoleForApi());
        const body = {
            prompt: (document.getElementById('config-prompt')?.value || '').trim(),
            rag: (document.getElementById('config-rag')?.value || '').trim(),
            greeting_text: (document.getElementById('config-greeting')?.value || '').trim(),
        };
        try {
            const res = await fetch(window.apiBase + `/api/tuning?role=${role}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : (data.detail || res.statusText));
            window.showToast('Configuration saved.', 'success');
        } catch (e) {
            window.showToast('Save failed: ' + (e.message || e), 'error');
        }
    };

    // ── Campaign Cases ──
    window._editingCaseId = null;
    window.loadCases = async function() {
        const role = encodeURIComponent(window.dashRoleForApi());
        const container = document.getElementById('cases-list');
        if (!container) return;
        try {
            const res = await fetch(window.apiBase + `/api/cases?role=${role}`, { cache: 'no-store' });
            if (!res.ok) throw new Error('failed');
            const data = await res.json();
            const cases = data.cases || [];
            const activeId = data.active_case_id;
            const pill = document.getElementById('cases-active-pill');
            if (pill) {
                const active = cases.find(c => c.id === activeId);
                pill.textContent = active ? `Active: ${active.name}` : 'No active case';
            }
            if (!cases.length) {
                container.innerHTML = '<div class="text-sm text-on-surface-variant text-center py-6 border border-dashed border-surface-container rounded-xl">No cases yet. Click "+ New Case" to create one.</div>';
                return;
            }
            container.innerHTML = cases.map(c => `
                <div class="flex items-center justify-between gap-3 p-3 rounded-lg border ${c.id === activeId ? 'border-primary bg-primary/5' : 'border-surface-container bg-surface-container-low'}">
                    <div class="flex-1 min-w-0">
                        <div class="flex items-center gap-2">
                            <span class="text-sm font-semibold text-on-surface">${c.name}</span>
                            ${c.id === activeId ? '<span class="text-[9px] font-bold uppercase bg-primary text-on-primary px-1.5 py-0.5 rounded-full">Active</span>' : ''}
                        </div>
                        <p class="text-xs text-on-surface-variant truncate mt-0.5">${c.description || 'No description'}</p>
                    </div>
                    <div class="flex items-center gap-2">
                        <button class="text-[10px] font-bold px-2.5 py-1 rounded-md ${c.id === activeId ? 'bg-surface-container-highest text-on-surface-variant' : 'bg-primary text-on-primary'}" onclick="window.activateCase(${c.id})">${c.id === activeId ? 'Active' : 'Activate'}</button>
                        <button class="text-on-surface-variant hover:text-primary p-1" onclick="window.editCase(${c.id}, '${encodeURIComponent(c.name)}', '${encodeURIComponent(c.description || '')}')" title="Edit"><span class="material-symbols-outlined text-base">edit</span></button>
                        <button class="text-on-surface-variant hover:text-error p-1" onclick="window.deleteCase(${c.id})" title="Delete"><span class="material-symbols-outlined text-base">delete</span></button>
                    </div>
                </div>
            `).join('');
        } catch (e) {
            if (container) container.innerHTML = '<div class="text-sm text-error text-center py-4">Failed to load cases.</div>';
        }
    };

    window.openCaseModal = function() {
        window._editingCaseId = null;
        const title = document.getElementById('case-modal-title');
        if (title) title.textContent = 'New Campaign Case';
        const nameInput = document.getElementById('case-name-input');
        const descInput = document.getElementById('case-desc-input');
        if (nameInput) nameInput.value = '';
        if (descInput) descInput.value = '';
        window.openModal('modal-case');
    };

    window.editCase = function(id, encName, encDesc) {
        window._editingCaseId = id;
        const title = document.getElementById('case-modal-title');
        if (title) title.textContent = 'Edit Campaign Case';
        const nameInput = document.getElementById('case-name-input');
        const descInput = document.getElementById('case-desc-input');
        if (nameInput) nameInput.value = decodeURIComponent(encName);
        if (descInput) descInput.value = decodeURIComponent(encDesc);
        window.openModal('modal-case');
    };

    window.saveCase = async function() {
        const role = encodeURIComponent(window.dashRoleForApi());
        const name = (document.getElementById('case-name-input')?.value || '').trim();
        const description = (document.getElementById('case-desc-input')?.value || '').trim();
        if (!name) { window.showToast('Case name is required.', 'error'); return; }
        try {
            if (window._editingCaseId) {
                const res = await fetch(window.apiBase + `/api/cases/${window._editingCaseId}?role=${role}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, description }),
                });
                if (!res.ok) throw new Error('Update failed');
            } else {
                const res = await fetch(window.apiBase + `/api/cases?role=${role}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, description }),
                });
                if (!res.ok) throw new Error('Create failed');
            }
            window.showToast('Case saved.', 'success');
            window.closeModal('modal-case');
            window.loadCases();
        } catch (e) {
            window.showToast('Save failed: ' + (e.message || e), 'error');
        }
    };

    window.activateCase = async function(caseId) {
        const role = encodeURIComponent(window.dashRoleForApi());
        try {
            const res = await fetch(window.apiBase + `/api/cases/${caseId}/activate?role=${role}`, { method: 'POST' });
            if (!res.ok) throw new Error('Activate failed');
            window.showToast('Case activated.', 'success');
            window.loadCases();
        } catch (e) {
            window.showToast('Activate failed: ' + (e.message || e), 'error');
        }
    };

    window.deleteCase = async function(caseId) {
        if (!confirm('Delete this case?')) return;
        const role = encodeURIComponent(window.dashRoleForApi());
        try {
            const res = await fetch(window.apiBase + `/api/cases/${caseId}?role=${role}`, { method: 'DELETE' });
            if (!res.ok) throw new Error('Delete failed');
            window.showToast('Case deleted.', 'success');
            window.loadCases();
        } catch (e) {
            window.showToast('Delete failed: ' + (e.message || e), 'error');
        }
    };

    // ── RAG Document Upload ──
    window.uploadRagDocs = async function(files) {
        if (!files || !files.length) return;
        const role = encodeURIComponent(window.dashRoleForApi());
        const statusEl = document.getElementById('rag-status-label');
        if (statusEl) statusEl.textContent = 'Uploading...';
        try {
            for (const file of files) {
                const fd = new FormData();
                fd.append('file', file);
                await fetch(window.apiBase + `/api/tuning/upload-doc?role=${role}`, { method: 'POST', body: fd });
            }
            if (statusEl) statusEl.textContent = 'READY';
            window.showToast('Documents uploaded.', 'success');
            window.loadConfigSettings();
        } catch (e) {
            if (statusEl) statusEl.textContent = 'ERROR';
            window.showToast('Upload failed: ' + (e.message || e), 'error');
        }
    };

    // ── Campaign Status Polling ──
     window.pollCampaignStatus = async function() {
         try {
             const role = encodeURIComponent(window.dashRoleForApi());
             const res = await fetch(window.apiBase + `/api/campaign/state?role=${role}&_skip_cache=true`, { cache: 'no-store' });
             if (!res.ok) return;
             const data = await res.json();
             const dot = document.getElementById('campaign-status-dot');
             const text = document.getElementById('campaign-status-text');
             if (!dot || !text) return;
             if (data.active && !data.campaign_paused) {
                 dot.className = 'w-2 h-2 rounded-full bg-tertiary pulse-active';
                 text.textContent = 'Calling…';
                 text.className = 'text-tertiary';
             } else if (data.campaign_paused) {
                 dot.className = 'w-2 h-2 rounded-full bg-outline-variant';
                 text.textContent = 'Paused';
                 text.className = 'text-on-surface-variant';
             } else {
                 dot.className = 'w-2 h-2 rounded-full bg-outline-variant';
                 text.textContent = 'Idle';
                 text.className = 'text-on-surface-variant';
             }
         } catch (_) {}
     };

    // --- Setup Listeners on Document Ready ---
    document.addEventListener('DOMContentLoaded', async () => {
         // Initialize UI (real data first, then render)
         window.appState.dataLoadedFromApi = await window.loadRealLeads();
         window.updateDashboardUI();

         // Set header role name
         const roleName = document.getElementById('header-role-name');
         if (roleName) roleName.textContent = window.dashRoleForApi();

         // Preload campaign sources in the background (lazy first paint for the other views).
         setTimeout(() => { window.loadCampaigns(); }, 400);

         // Poll campaign status every 5 seconds
         window.pollCampaignStatus();
         setInterval(() => { window.pollCampaignStatus(); }, 5000);

        // 1. Sidebar views navigation
        const links = document.querySelectorAll('.nav-link');
        links.forEach(link => {
            link.addEventListener('click', (e) => {
                e.preventDefault();
                const view = link.getAttribute('data-view');
                window.switchView(view);
            });
        });

        // 3. Sandbox Selection buttons
        const sandBtns = document.querySelectorAll('.sandbox-btn');
        sandBtns.forEach((btn, idx) => {
            btn.addEventListener('click', () => {
                sandBtns.forEach(b => b.className = "sandbox-btn w-full py-1.5 text-on-surface-variant rounded-lg text-body-sm font-body-sm text-center hover:bg-surface-container transition-all");
                btn.className = "sandbox-btn w-full py-1.5 bg-primary text-on-primary rounded-lg text-body-sm font-body-sm font-medium text-center shadow-sm transition-all";
                window.appState.currentSandbox = idx + 1;
                window.appState.campaignSandbox = idx + 1;
                window.showToast(`Active sandbox changed to Sandbox ${window.appState.currentSandbox}`, 'info');
                window.updateDashboardUI();
                window.renderCallLogsTable();
            });
        });

        // 3b. Source type selection (Cold vs Digital)
        const sourceBtns = document.querySelectorAll('.camp-source-btn');
        sourceBtns.forEach(btn => {
            btn.addEventListener('click', () => {
                const src = btn.dataset.source;
                window.setLeadSource(src);
            });
        });

        // 4. Quick Filters buttons
        const qfBtns = document.querySelectorAll('.qf-btn');
        qfBtns.forEach(btn => {
            btn.addEventListener('click', () => {
                qfBtns.forEach(b => b.className = "px-3 py-1 border border-outline-variant text-on-surface-variant rounded-full text-label-sm font-label-sm uppercase hover:bg-surface-container");
                btn.className = "px-3 py-1 bg-primary text-on-primary rounded-full text-label-sm font-label-sm uppercase";
                window.appState.currentFilter = btn.getAttribute('data-filter');
                window.renderCallLogsTable();
            });
        });

        // 5. Table Live Search
        const searchInput = document.getElementById('logs-search-input');
        if (searchInput) {
            searchInput.addEventListener('input', (e) => {
                window.appState.tableSearch = e.target.value;
                window.renderCallLogsTable();
            });
        }

        // 6. Location / Budget filters
         const selectLoc = document.getElementById('filter-location');
         const selectBud = document.getElementById('filter-budget');
         if (selectLoc) selectLoc.addEventListener('change', window.renderCallLogsTable);
         if (selectBud) selectBud.addEventListener('change', window.renderCallLogsTable);

         // 6b. Date range filter
         const dateFrom = document.getElementById('filter-date-from');
         const dateTo = document.getElementById('filter-date-to');
         if (dateFrom) dateFrom.addEventListener('change', window.renderCallLogsTable);
         if (dateTo) dateTo.addEventListener('change', window.renderCallLogsTable);

        // 7. Dark Mode Toggle
        const themeBtn = document.getElementById('theme-toggle-btn');
        if (themeBtn) {
            themeBtn.addEventListener('click', () => {
                const html = document.documentElement;
                const isDark = html.classList.toggle('dark');
                localStorage.setItem('theme', isDark ? 'dark' : 'light');
                themeBtn.querySelector('.material-symbols-outlined').textContent = isDark ? 'light_mode' : 'dark_mode';
                themeBtn.querySelector('span:not(.material-symbols-outlined)').textContent = isDark ? 'Light Mode' : 'Dark Mode';
                window.showToast(isDark ? 'Dark mode enabled' : 'Light mode enabled', 'info');
            });

            // Initial theme recovery
            if (localStorage.getItem('theme') === 'dark') {
                document.documentElement.classList.add('dark');
                themeBtn.querySelector('.material-symbols-outlined').textContent = 'light_mode';
                themeBtn.querySelector('span:not(.material-symbols-outlined)').textContent = 'Light Mode';
            }
        }

        // 8. Engagement Timeline hover tooltip interactions
        const overlay = document.getElementById('chart-hover-overlay');
        const tooltipCard = document.getElementById('chart-tooltip-card');
        const tooltipLine = document.getElementById('chart-tooltip-line');
        const dotCalls = document.getElementById('chart-tooltip-dot-calls');
        const dotInt = document.getElementById('chart-tooltip-dot-int');

        if (overlay && tooltipCard) {
            overlay.addEventListener('mousemove', (e) => {
                const data = window.chartCurrentData;
                if (!data) return;

                // Get relative X position
                const rect = overlay.getBoundingClientRect();
                const mouseX = e.clientX - rect.left;
                const relativeX = mouseX / rect.width; // 0 to 1
                
                // Map to closest point (0 to 6)
                const points = 7;
                let idx = Math.round(relativeX * (points - 1));
                if (idx < 0) idx = 0;
                if (idx >= points) idx = points - 1;

                // Calculate visual coordinates
                const xStep = 800 / (points - 1);
                const visualX = idx * xStep;
                const yCall = 230 - (data.calls[idx] / data.maxVal) * 180;
                const yInt = 230 - (data.interested[idx] / data.maxVal) * 180;

                // Update tooltip line
                if (tooltipLine) {
                    tooltipLine.setAttribute('x1', visualX);
                    tooltipLine.setAttribute('x2', visualX);
                    tooltipLine.style.display = 'block';
                }

                // Update dots
                if (dotCalls) {
                    dotCalls.setAttribute('cx', visualX);
                    dotCalls.setAttribute('cy', yCall);
                    dotCalls.style.display = 'block';
                }
                if (dotInt) {
                    dotInt.setAttribute('cx', visualX);
                    dotInt.setAttribute('cy', yInt);
                    dotInt.style.display = 'block';
                }

                // Update Card content
                const dateEl = document.getElementById('chart-tooltip-date');
                const valCalls = document.getElementById('chart-tooltip-val-calls');
                const valInt = document.getElementById('chart-tooltip-val-int');

                if (dateEl) dateEl.textContent = `Day: ${data.days[idx]}`;
                if (valCalls) valCalls.textContent = data.calls[idx];
                if (valInt) valInt.textContent = data.interested[idx];

                // Position the Card (offset by some pixels)
                // Map visualX (0-800) back to client pixels relative to container
                const cardWidth = tooltipCard.offsetWidth || 140;
                const containerWidth = rect.width;
                let clientX = (visualX / 800) * containerWidth;
                
                // Keep tooltip within bounds
                if (clientX + cardWidth + 15 > containerWidth) {
                    clientX = clientX - cardWidth - 15;
                } else {
                    clientX = clientX + 15;
                }

                tooltipCard.style.left = `${clientX}px`;
                tooltipCard.style.top = '30px';
                tooltipCard.style.display = 'block';
            });

            overlay.addEventListener('mouseleave', () => {
                if (tooltipLine) tooltipLine.style.display = 'none';
                if (dotCalls) dotCalls.style.display = 'none';
                if (dotInt) dotInt.style.display = 'none';
                tooltipCard.style.display = 'none';
            });
        }

        // 9. Donut Chart hover segment interactions
        const donutTooltipCard = document.getElementById('donut-tooltip-card');
        const donutTooltipTitle = document.getElementById('donut-tooltip-title');
        const donutTooltipColor = document.getElementById('donut-tooltip-color');
        const donutTooltipVal = document.getElementById('donut-tooltip-val');
        const donutCenterLabel = document.getElementById('donut-center-label');
        const donutCenterCount = document.getElementById('donut-center-count');

        const donutCategories = {
            'donut-seg-1': { label: 'Interested', color: '#bd0917', key: 'interested' },
            'donut-seg-2': { label: 'Failed', color: '#e5bdb9', key: 'failed' },
            'donut-seg-3': { label: 'Call Later', color: '#5cb1ff', key: 'callbacks' },
            'donut-seg-4': { label: 'Answered', color: '#00857f', key: 'answered' }
        };

        Object.keys(donutCategories).forEach(segId => {
            const segEl = document.getElementById(segId);
            if (segEl) {
                const cat = donutCategories[segId];
                
                segEl.addEventListener('mouseenter', (e) => {
                    const data = window.donutCurrentData;
                    if (!data) return;
                    
                    const value = data[cat.key];
                    
                    // Style seg element (expand stroke width to 18px)
                    segEl.setAttribute('stroke-width', '18');
                    
                    // Update Donut Center Text
                    if (donutCenterLabel) donutCenterLabel.textContent = cat.label;
                    if (donutCenterCount) donutCenterCount.textContent = value.toLocaleString();
                    
                    // Update Tooltip Card
                    if (donutTooltipCard) {
                        if (donutTooltipTitle) donutTooltipTitle.textContent = cat.label;
                        if (donutTooltipColor) donutTooltipColor.style.backgroundColor = cat.color;
                        if (donutTooltipVal) donutTooltipVal.textContent = value;
                        
                        // Position relative to mouse client position within the chart relative box
                        const parentRect = donutTooltipCard.parentElement.getBoundingClientRect();
                        const cardX = e.clientX - parentRect.left + 15;
                        const cardY = e.clientY - parentRect.top + 15;
                        
                        donutTooltipCard.style.left = `${cardX}px`;
                        donutTooltipCard.style.top = `${cardY}px`;
                        donutTooltipCard.style.display = 'block';
                    }
                });
                
                segEl.addEventListener('mousemove', (e) => {
                    if (donutTooltipCard) {
                        const parentRect = donutTooltipCard.parentElement.getBoundingClientRect();
                        const cardX = e.clientX - parentRect.left + 15;
                        const cardY = e.clientY - parentRect.top + 15;
                        
                        donutTooltipCard.style.left = `${cardX}px`;
                        donutTooltipCard.style.top = `${cardY}px`;
                    }
                });

                segEl.addEventListener('mouseleave', () => {
                    segEl.setAttribute('stroke-width', '15');
                    
                    // Restore center text to sum of outcomes
                    const data = window.donutCurrentData;
                    if (data) {
                        if (donutCenterLabel) donutCenterLabel.textContent = 'Total';
                        if (donutCenterCount) donutCenterCount.textContent = data.total.toLocaleString();
                    }
                    
                    if (donutTooltipCard) donutTooltipCard.style.display = 'none';
                });
            }
        });

        // 10. Hourly Distribution bar hover interactions
        const barTooltipCard = document.getElementById('bar-tooltip-card');
        const barTooltipHour = document.getElementById('bar-tooltip-hour');
        const barTooltipVal = document.getElementById('bar-tooltip-val');
        
        const hourIntervals = [
            "12:00 AM - 2:00 AM",
            "2:00 AM - 4:00 AM",
            "4:00 AM - 6:00 AM",
            "6:00 AM - 8:00 AM",
            "8:00 AM - 10:00 AM",
            "10:00 AM - 12:00 PM",
            "12:00 PM - 2:00 PM",
            "2:00 PM - 4:00 PM",
            "4:00 PM - 6:00 PM",
            "6:00 PM - 8:00 PM",
            "8:00 PM - 10:00 PM",
            "10:00 PM - 12:00 AM"
        ];
        
        const barElements = document.querySelectorAll('.chart-bar');
        barElements.forEach(bar => {
            bar.addEventListener('mouseenter', (e) => {
                const idxStr = bar.getAttribute('data-bar-idx');
                const idx = parseInt(idxStr, 10);
                if (isNaN(idx)) return;
                
                const data = window.barCurrentData;
                if (!data) return;
                
                const value = data.hours[idx];
                
                // Highlight bar
                bar.style.opacity = '0.8';
                
                // Update and show Tooltip Card
                if (barTooltipCard) {
                    if (barTooltipHour) barTooltipHour.textContent = hourIntervals[idx];
                    if (barTooltipVal) barTooltipVal.textContent = value;
                    
                    // Position over the bar
                    const parentRect = barTooltipCard.parentElement.getBoundingClientRect();
                    const barRect = bar.getBoundingClientRect();
                    
                    // Position tooltip centered above the bar
                    const cardWidth = barTooltipCard.offsetWidth || 130;
                    const cardHeight = barTooltipCard.offsetHeight || 50;
                    
                    const cardX = (barRect.left - parentRect.left) + (barRect.width / 2) - (cardWidth / 2);
                    const cardY = (barRect.top - parentRect.top) - cardHeight - 10;
                    
                    barTooltipCard.style.left = `${Math.max(10, cardX)}px`;
                    barTooltipCard.style.top = `${Math.max(10, cardY)}px`;
                    barTooltipCard.style.display = 'block';
                }
            });
            
            bar.addEventListener('mouseleave', () => {
                bar.style.opacity = '1';
                if (barTooltipCard) barTooltipCard.style.display = 'none';
            });
        });
    });

})();
