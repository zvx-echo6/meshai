import { useState, useEffect, useRef } from 'react'
import { useLocation } from 'react-router-dom'
import {
  Search, Droplets, Flame, Satellite, CloudLightning, Sun,
  Radio, Mountain, Car, Construction, Activity, Bell, Terminal,
  Code, ExternalLink,
  Crosshair, Send, Clock, MessageSquare, Network, Sliders,
  Database, History
} from 'lucide-react'

// Topic definitions
const TOPICS = [
  { id: 'stream-gauges', label: 'Stream Gauges', icon: Droplets },
  { id: 'wildfire', label: 'Wildfire', icon: Flame },
  { id: 'firms', label: 'Satellite Fire Detection (FIRMS)', icon: Satellite },
  { id: 'fire-tracker', label: 'Fire Tracker (Fusion)', icon: Crosshair },
  { id: 'weather-alerts', label: 'Weather Alerts', icon: CloudLightning },
  { id: 'solar', label: 'Solar & Geomagnetic', icon: Sun },
  { id: 'ducting', label: 'Tropospheric Ducting', icon: Radio },
  { id: 'avalanche', label: 'Avalanche Danger', icon: Mountain },
  { id: 'traffic', label: 'Traffic Flow', icon: Car },
  { id: 'roads-511', label: 'Road Conditions (511)', icon: Construction },
  { id: 'mesh-health', label: 'Mesh Health', icon: Activity },
  { id: 'broadcast-types', label: 'Broadcast Types', icon: Send },
  { id: 'reminders', label: 'Reminder System', icon: Clock },
  { id: 'notifications', label: 'Notifications', icon: Bell },
  { id: 'commands', label: 'Commands', icon: Terminal },
  { id: 'llm-dm', label: 'LLM DM Queries', icon: MessageSquare },
  { id: 'or-not-and', label: 'OR-not-AND Architecture', icon: Network },
  { id: 'adapter-config', label: 'Adapter Config & CODE Rule', icon: Sliders },
  { id: 'curation', label: 'Curation: Gauges & Towns', icon: Database },
  { id: 'schema', label: 'Schema Migrations', icon: History },
  { id: 'api', label: 'API Reference', icon: Code },
]

// Status indicator component for colored dots
function StatusDot({ color }: { color: 'green' | 'yellow' | 'orange' | 'red' | 'black' }) {
  const colorClasses = {
    green: 'bg-green-500',
    yellow: 'bg-yellow-500',
    orange: 'bg-orange-500',
    red: 'bg-red-500',
    black: 'bg-slate-800 border border-slate-600',
  }
  return <span className={`inline-block w-3 h-3 rounded-full ${colorClasses[color]}`} />
}

// Table component styled for dark theme
function RefTable({ headers, rows }: { headers: string[]; rows: (string | React.ReactNode)[][] }) {
  return (
    <div className="overflow-x-auto my-4">
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-[#1a2332] border-b border-[#2a3a4a]">
            {headers.map((h, i) => (
              <th key={i} className="px-4 py-2 text-left text-slate-400 font-medium">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className={`border-b border-[#1e2a3a] ${i % 2 === 0 ? 'bg-[#0d1219]' : 'bg-[#0a0e17]'}`}>
              {row.map((cell, j) => (
                <td key={j} className="px-4 py-2 text-slate-300">{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// External link component
function ExtLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-accent hover:underline inline-flex items-center gap-1"
    >
      {children} <ExternalLink size={12} />
    </a>
  )
}

// Section header
function SectionHeader({ children }: { children: React.ReactNode }) {
  return <h3 className="text-lg font-semibold text-slate-200 mt-6 mb-3">{children}</h3>
}

// Sub-header
function SubHeader({ children }: { children: React.ReactNode }) {
  return <h4 className="text-base font-medium text-slate-300 mt-4 mb-2">{children}</h4>
}

// Monospace text
function Mono({ children }: { children: React.ReactNode }) {
  return <code className="font-mono text-accent bg-[#1a2332] px-1 rounded">{children}</code>
}

// Topic section wrapper
function TopicSection({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <section id={id} className="mb-12 scroll-mt-6">
      <h2 className="text-2xl font-bold text-slate-100 mb-4 pb-2 border-b border-[#2a3a4a]">{title}</h2>
      <div className="text-slate-300 leading-relaxed space-y-4">
        {children}
      </div>
    </section>
  )
}

export default function Reference() {
  const location = useLocation()
  const [searchQuery, setSearchQuery] = useState('')
  const [activeTopic, setActiveTopic] = useState('stream-gauges')
  const contentRef = useRef<HTMLDivElement>(null)

  // Handle hash navigation
  useEffect(() => {
    const hash = location.hash.replace('#', '')
    if (hash && TOPICS.find(t => t.id === hash)) {
      setActiveTopic(hash)
      const element = document.getElementById(hash)
      if (element) {
        element.scrollIntoView({ behavior: 'smooth' })
      }
    }
  }, [location.hash])

  // Filter topics by search
  const filteredTopics = TOPICS.filter(t =>
    t.label.toLowerCase().includes(searchQuery.toLowerCase())
  )

  const scrollToTopic = (topicId: string) => {
    setActiveTopic(topicId)
    const element = document.getElementById(topicId)
    if (element) {
      element.scrollIntoView({ behavior: 'smooth' })
    }
    window.history.replaceState(null, '', `#${topicId}`)
  }

  return (
    <div className="flex h-full -m-6">
      {/* Topic sidebar */}
      <aside className="w-64 flex-shrink-0 bg-bg-card border-r border-border overflow-y-auto">
        <div className="p-4 border-b border-border">
          <div className="relative">
            <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search topics..."
              className="w-full pl-9 pr-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 focus:outline-none focus:border-accent placeholder-slate-600"
            />
          </div>
        </div>
        <nav className="py-2">
          {filteredTopics.map((topic) => {
            const Icon = topic.icon
            const isActive = activeTopic === topic.id
            return (
              <button
                key={topic.id}
                onClick={() => scrollToTopic(topic.id)}
                className={`w-full flex items-center gap-3 px-4 py-2.5 text-sm text-left transition-colors ${
                  isActive
                    ? 'text-accent bg-accent/10 border-l-2 border-accent'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-bg-hover border-l-2 border-transparent'
                }`}
              >
                <Icon size={16} />
                {topic.label}
              </button>
            )
          })}
        </nav>
      </aside>

      {/* Main content */}
      <div ref={contentRef} className="flex-1 overflow-y-auto p-6">
        <div className="max-w-4xl">
          <p className="text-slate-400 mb-8">
            Everything you need to understand and configure MeshAI's monitoring and alerting systems.
          </p>

          {/* Stream Gauges */}
          <TopicSection id="stream-gauges" title="Stream Gauges">
            <SectionHeader>What You're Looking At</SectionHeader>
            <p>
              MeshAI watches river and stream levels at gauges you configure. Each gauge reports two things:
            </p>
            <p>
              <strong>Water Level (Gage Height)</strong> — how high the water is, measured in feet. Important: this is NOT the depth of the river. It's the height above a fixed measuring point that's different at every gauge. A reading of "10 feet" at one gauge means something completely different than "10 feet" at another. You can only compare readings from the SAME gauge over time.
            </p>
            <p>
              <strong>Flow (Discharge)</strong> — how much water is moving past the gauge, in cubic feet per second (CFS). Think of it as the river's "throughput." For scale:
            </p>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li>A small creek: 50-200 CFS</li>
              <li>A mid-size river: 1,000-5,000 CFS</li>
              <li>A big river in spring runoff: 10,000+ CFS</li>
            </ul>

            <SectionHeader>When Does It Flood?</SectionHeader>
            <p>
              Flood levels are set by the <strong>National Weather Service</strong>, not USGS. NWS looks at each specific gauge location and decides "at what water level does the road flood? At what level do buildings get water?" Those levels are different everywhere.
            </p>
            <p><strong>Action Stage</strong> — water is rising, time to start paying attention. Usually still inside the riverbanks.</p>
            <p><strong>Minor Flood</strong> — low-lying roads start getting water on them. NWS issues a Flood Advisory.</p>
            <p><strong>Moderate Flood</strong> — water in buildings near the river. Some people need to evacuate. NWS issues a Flood Warning.</p>
            <p><strong>Major Flood</strong> — widespread flooding. Many people evacuating. Serious property damage.</p>
            <p>
              MeshAI automatically looks up the flood levels for your gauge from NWS when you add a site. Some remote gauges don't have flood levels assigned — for those, you set them manually if you know what water levels cause problems in your area.
            </p>

            <SectionHeader>Low Water / Drought</SectionHeader>
            <p>
              There's no official "drought stage" for most gauges. If you need to monitor low water (irrigation, fish habitat), set a manual low-water threshold based on what you know about your local river.
            </p>

            <SectionHeader>Setting It Up</SectionHeader>
            <ol className="list-decimal list-inside ml-4 space-y-1">
              <li>Find your gauge at <ExtLink href="https://waterdata.usgs.gov/nwis">waterdata.usgs.gov/nwis</ExtLink></li>
              <li>Copy the site number (like <Mono>13090500</Mono>)</li>
              <li>Add it in Config → Environmental → USGS</li>
              <li>MeshAI auto-fills the gauge name and flood levels from NWS</li>
            </ol>
            <p>If NWS flood levels don't populate, your gauge may not have them. Set manual thresholds if you know your local conditions.</p>

            <SectionHeader>Learn More</SectionHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><ExtLink href="https://waterdata.usgs.gov/nwis">USGS Water Data</ExtLink> — find gauges near you</li>
              <li><ExtLink href="https://water.noaa.gov">NWS Water Prediction Service</ExtLink> — flood forecasts and thresholds</li>
              <li><ExtLink href="https://www.usgs.gov/special-topics/water-science-school/science/how-streamflow-measured">Understanding Streamflow</ExtLink> — USGS explainer</li>
            </ul>
          </TopicSection>

          {/* Wildfire */}
          <TopicSection id="wildfire" title="Wildfire">
            <SectionHeader>What You're Looking At</SectionHeader>
            <p>
              MeshAI tracks active wildfire perimeters from the National Interagency Fire Center (NIFC). For each fire, you see the name, size, how much is contained, and how far it is from your mesh nodes.
            </p>

            <SectionHeader>Fire Size — How Big Is It?</SectionHeader>
            <RefTable
              headers={['Size', 'What That Means']}
              rows={[
                ['10 acres', 'Small fire. Usually handled quickly by initial crews.'],
                ['100 acres', 'Notable fire. Active firefighting effort.'],
                ['1,000 acres', 'Large fire. Major resources being deployed.'],
                ['10,000+ acres', 'Very large fire. Multiple teams, aircraft, heavy equipment.'],
                ['100,000+ acres', 'Mega-fire. These make the national news.'],
              ]}
            />
            <p>For reference, 1,000 acres is about 1.5 square miles.</p>

            <SectionHeader>Containment — Is It Under Control?</SectionHeader>
            <p>
              Containment means the percentage of the fire's edge where firefighters have built a control line (a cleared strip to stop the fire from spreading further). It does NOT mean the fire is out inside that line.
            </p>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><strong>0-30%</strong> — Essentially uncontrolled. The fire goes where it wants.</li>
              <li><strong>50%</strong> — Good progress, but half the edge can still grow.</li>
              <li><strong>80%+</strong> — Well controlled. Major growth unlikely.</li>
              <li><strong>100%</strong> — The edge is fully controlled. But the fire may STILL be actively burning inside. "100% contained" does NOT mean "out."</li>
            </ul>

            <SectionHeader>How Far Away Should I Worry?</SectionHeader>
            <RefTable
              headers={['Distance', 'What To Do']}
              rows={[
                [<><StatusDot color="red" /> Under 5 km (3 miles)</>, <><strong>Immediate threat.</strong> This is evacuation-order range. Embers can fly this far in wind.</>],
                [<><StatusDot color="orange" /> 5-15 km (3-10 miles)</>, <><strong>Prepare.</strong> The fire could reach you in hours under bad conditions. Have a plan.</>],
                [<><StatusDot color="yellow" /> 15-30 km (10-20 miles)</>, <><strong>Watch.</strong> Smoke is likely. Wind shifts could change things fast.</>],
                [<><StatusDot color="green" /> Over 30 km (20 miles)</>, <><strong>Awareness.</strong> Keep an eye on it, but no immediate threat.</>],
              ]}
            />
            <p>
              How fast can a fire travel? In grass with wind: up to 14 mph. In heavy timber: 1-6 mph. A fire 10 miles away could theoretically reach you in 1-2 hours under worst-case conditions, but typical spread is much slower.
            </p>

            <SectionHeader>Which Matters More — Size or Distance?</SectionHeader>
            <p>
              <strong>Distance is the immediate concern.</strong> A small uncontained fire 10 km away is more dangerous right now than a huge fire 50 km away. But big fires have more energy and can grow fast under wind shifts — keep watching them.
            </p>

            <SectionHeader>Setting It Up</SectionHeader>
            <p>
              Just configure your state code (like <Mono>US-ID</Mono> for Idaho) in Config → Environmental → Fires. MeshAI polls NIFC every 10 minutes for active fires in that state and computes the distance to your mesh nodes automatically.
            </p>

            <SectionHeader>Learn More</SectionHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><ExtLink href="https://inciweb.nwcg.gov">InciWeb</ExtLink> — detailed incident information</li>
              <li><ExtLink href="https://data-nifc.opendata.arcgis.com">NIFC Fire Map</ExtLink> — raw perimeter data</li>
              <li><ExtLink href="https://www.ready.gov/wildfires">Ready.gov Wildfires</ExtLink> — preparedness guide</li>
            </ul>
          </TopicSection>

          {/* FIRMS */}
          <TopicSection id="firms" title="Satellite Fire Detection (FIRMS)">
            <SectionHeader>What You're Looking At</SectionHeader>
            <p>
              NASA's VIIRS satellites orbit the Earth and look for heat signatures on the ground. When they see something hot — a fire, a factory, a sunlit building — they flag it as a "hotspot." MeshAI checks these detections for your area.
            </p>
            <p>
              <strong>Why this matters</strong>: satellite hotspots show up <strong>hours before</strong> official fire perimeters are mapped. If a new fire starts near your mesh, the satellite might see it before anyone on the ground reports it.
            </p>

            <SectionHeader>Confidence — Is It Really a Fire?</SectionHeader>
            <p>Each detection gets a confidence rating:</p>
            <RefTable
              headers={['Confidence', 'What It Means']}
              rows={[
                ['High', 'Almost certainly a real fire. Strong heat signature.'],
                ['Nominal', 'Probably a real fire. Most actual fires get this rating.'],
                ['Low', 'Maybe a fire, maybe not. Could be a hot roof, sun reflecting off water, a factory, or a gas flare. Lots of false alarms.'],
              ]}
            />
            <p>
              <strong>Recommendation</strong>: Set the filter to "Nominal + High." If you include "Low" you'll get alerts for every hot parking lot on a summer day.
            </p>

            <SectionHeader>FRP — How Intense Is It?</SectionHeader>
            <p>FRP (Fire Radiative Power) measures the heat output in megawatts. Think of it as "how hot is this thing":</p>
            <RefTable
              headers={['FRP', 'What It Probably Is']}
              rows={[
                ['Under 5 MW', 'Hot surface, small agricultural burn, gas flare, or warm ground'],
                ['5-50 MW', 'An actual fire — brush fire, grass fire, typical wildfire'],
                ['50-300 MW', 'Intense fire — trees fully burning, active fire front'],
                ['Over 300 MW', 'Extreme fire — major wildfire in full force'],
              ]}
            />
            <p>Setting the minimum FRP to 5 MW filters out most industrial and agricultural false alarms.</p>

            <SectionHeader>New Ignition Detection</SectionHeader>
            <p>
              MeshAI cross-references satellite hotspots against known NIFC fire perimeters. If a hotspot is NOT near any known fire, it gets flagged as a <strong>potential new ignition</strong> — maybe a new fire just started. These get elevated priority regardless of confidence level.
            </p>

            <SectionHeader>Timing</SectionHeader>
            <p>
              Satellite data arrives <strong>1-3 hours</strong> after the satellite passes overhead. Each location gets observed about <strong>6 times per day</strong> across all satellites, so there are multi-hour gaps. This is not real-time — it's "pretty recent."
            </p>

            <SectionHeader>Getting an API Key</SectionHeader>
            <ol className="list-decimal list-inside ml-4 space-y-1">
              <li>Go to <ExtLink href="https://firms.modaps.eosdis.nasa.gov/api/area/">FIRMS API page</ExtLink></li>
              <li>Click "Get MAP_KEY"</li>
              <li>Register for a free Earthdata account</li>
              <li>Your key arrives by email</li>
              <li>Enter it in Config → Environmental → FIRMS</li>
            </ol>

            <SectionHeader>Learn More</SectionHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><ExtLink href="https://firms.modaps.eosdis.nasa.gov">FIRMS Fire Map</ExtLink> — see hotspots on a map</li>
              <li><ExtLink href="https://earthdata.nasa.gov/data/tools/firms/faq">FIRMS FAQ</ExtLink> — how it works</li>
            </ul>
          </TopicSection>

          {/* Fire Tracker (v0.7 fusion: FIRMS + WFIGS + LLM digest) */}
          <TopicSection id="fire-tracker" title="Fire Tracker (Fusion)">
            <p>
              FIRMS hotspots are fast but noisy; WFIGS incidents are accurate but slow.
              The Fire Tracker fuses both feeds and a per-pixel attribution graph so a
              single fire's name, declared acreage, real-time perimeter movement, and
              spotting events all land as separate broadcasts on the mesh.
            </p>

            <SectionHeader>What you'll see on the mesh</SectionHeader>
            <p>Six fire-family alert categories, in order of when they fire during an incident's lifecycle:</p>
            <RefTable
              headers={['Category', 'Severity', 'Trigger', 'Example broadcast']}
              rows={[
                [<Mono>unattributed_hotspot_cluster</Mono>, "Priority",
                  "3+ FIRMS pixels within 1 mi over 60 min, no WFIGS match — possible new ignition before NIFC declares it",
                  <span className="text-amber-300">🔥 Possible new fire: 3 hotspots within 1 mi @ 42.93,-114.45 (combined 78 MW)</span>],
                [<Mono>wildfire_declared</Mono>, "Priority",
                  "WFIGS first-sight of a new IRWIN incident — the official 'this is a fire and here is its name' record",
                  <span className="text-amber-300">🔥 New: Cache Peak Fire (WF), 3 mi N of Almo: 250 ac, 0% contained</span>],
                [<Mono>wildfire_growth</Mono>, "Priority",
                  "Per-pass centroid drift >= 0.5 mi (configurable) between consecutive satellite passes — the fire's footprint moved",
                  <span className="text-amber-300">🔥 Cache Peak Fire moving NE 1.2 mi/h, ~3 mi from Almo</span>],
                [<Mono>wildfire_spotting</Mono>, "Immediate",
                  "FIRMS pixel attributed to a tracked fire but >= 1.5 mi (configurable) outside its prior-pass convex-hull perimeter — ember spread",
                  <span className="text-amber-300">🔥 Possible spotting 2.1 mi NE of Cache Peak Fire perimeter</span>],
                [<Mono>wildfire_incident</Mono>, "Priority",
                  "WFIGS acreage or containment increased on a fire already broadcast once (the Update path; the New path uses wildfire_declared)",
                  <span className="text-amber-300">🔥 Update: Cache Peak Fire: 1,847 ac, 23% contained</span>],
                [<Mono>wildfire_halted</Mono>, "Routine",
                  "No FIRMS pixels attributed for 12+ hours (configurable) — fire stalled or out",
                  <span className="text-amber-300">🔥 Cache Peak Fire no growth in 14h</span>],
              ]}
            />

            <SectionHeader>Daily LLM digest</SectionHeader>
            <p>
              Twice a day (default 06:00 and 18:00 Mountain Time) the bot runs an LLM
              summary across every active fire and the last 24 h of growth + spotting
              events, then broadcasts one terse line to the mesh. Shape:{' '}
              <span className="text-amber-300">"Fires today: Cache Peak 1,847 ac +200 NE; Twin Peaks 320 ac stable; possible new fire 15 mi from Cache Peak."</span>{' '}
              Configure the schedule and timezone under <Mono>fires.digest_*</Mono>{' '}
              keys on the Adapter Config page.
            </p>

            <SectionHeader>How attribution works</SectionHeader>
            <p>
              When a FIRMS hotspot lands, the bot walks every active fire (those not
              yet tombstoned) and matches by Haversine distance to that fire's running
              centroid. If the pixel is within the fire's <Mono>spread_radius_mi</Mono>{' '}
              (default 5 mi, per-fire override available) the pixel is attributed and
              appended to that fire's growth history. The centroid then re-computes
              as the median of the last 24 h of attributed pixels, so single-pixel
              outliers don't drag the perimeter around.
            </p>
            <p>
              Pixels that match no fire feed the cluster detector instead: if at least{' '}
              <Mono>cluster_min_pixels</Mono> (default 3) lie within{' '}
              <Mono>cluster_max_radius_mi</Mono> (default 1.0) over{' '}
              <Mono>cluster_time_window_minutes</Mono> (default 60), the bot fires a
              single <Mono>unattributed_hotspot_cluster</Mono> broadcast and marks
              the member pixels so a fourth arrival doesn't re-fire the same cluster.
            </p>

            <SectionHeader>How movement is computed</SectionHeader>
            <p>
              Each VIIRS pass groups pixels into a <Mono>pass_id</Mono> (satellite +
              90-min bucket). When a pixel from a different bucket arrives, the prior
              pass closes: its convex hull becomes the perimeter, its median centroid
              becomes the comparison anchor, and the bot computes drift (Haversine to
              the previous pass's centroid), an 8-way compass bearing, and a wall-clock
              mi/h speed. If drift &ge; <Mono>growth_drift_threshold_mi</Mono> the{' '}
              <Mono>wildfire_growth</Mono> broadcast fires.
            </p>

            <SectionHeader>How spotting is detected</SectionHeader>
            <p>
              Once a pass closes its perimeter (a GeoJSON polygon stored on the
              fire), every subsequent attributed pixel runs a point-in-polygon test.
              Pixels outside the polygon with a vertex distance &ge;{' '}
              <Mono>spotting_distance_threshold_mi</Mono> (default 1.5) fire the{' '}
              <Mono>wildfire_spotting</Mono> broadcast at <em>immediate</em> severity
              — spread beyond the existing perimeter is the most actionable fire
              signal we emit. A per-fire cooldown
              (<Mono>spotting_cooldown_seconds</Mono>, default 1 h) prevents an ember
              burst in the same area from spamming the mesh.
            </p>

            <SectionHeader>Tunable knobs (Adapter Config → fires)</SectionHeader>
            <RefTable
              headers={['Key', 'Default', 'What it does']}
              rows={[
                [<Mono>spread_radius_mi_default</Mono>, '5.0 mi', 'Attribution radius for FIRMS → fire matching. Per-fire override in the fires.spread_radius_mi column.'],
                [<Mono>growth_drift_threshold_mi</Mono>, '0.5 mi', 'Per-pass centroid drift at or above this fires wildfire_growth.'],
                [<Mono>halt_passes_threshold</Mono>, '2', 'Consecutive empty satellite passes before wildfire_halted (documented; the time gate below is the operational rule).'],
                [<Mono>halt_minimum_seconds</Mono>, '43,200 (12 h)', 'Minimum elapsed seconds since the most recent attributed pixel before wildfire_halted can fire.'],
                [<Mono>spotting_distance_threshold_mi</Mono>, '1.5 mi', 'Distance from prior-pass perimeter that fires wildfire_spotting.'],
                [<Mono>spotting_cooldown_seconds</Mono>, '3,600 (1 h)', 'Minimum seconds between consecutive spotting broadcasts per fire.'],
                [<Mono>digest_enabled</Mono>, 'true', 'Master toggle for the twice-daily digest.'],
                [<Mono>digest_schedule</Mono>, '["06:00","18:00"]', 'Local-time slots for the digest.'],
                [<Mono>digest_timezone</Mono>, 'America/Boise', 'IANA tz for digest_schedule.'],
                [<Mono>digest_max_chars</Mono>, '200', 'Hard cap on the digest wire (the LLM is told to fit; the chunker enforces).'],
              ]}
            />
          </TopicSection>

          {/* Weather Alerts */}
          <TopicSection id="weather-alerts" title="Weather Alerts">
            <SectionHeader>What You're Looking At</SectionHeader>
            <p>
              MeshAI watches for NWS (National Weather Service) alerts affecting your area — warnings, watches, and advisories.
            </p>

            <SectionHeader>Alert Severity — How Serious Is It?</SectionHeader>
            <RefTable
              headers={['Severity', 'What It Means', 'Example']}
              rows={[
                ['Extreme', 'Life-threatening. The most serious events.', 'Tornado Emergency, Hurricane Warning, Tsunami Warning'],
                ['Severe', 'Dangerous. Take protective action.', 'Tornado Warning, Flash Flood Warning, Blizzard Warning, Red Flag Warning'],
                ['Moderate', 'Be prepared. Could become dangerous.', 'Winter Weather Advisory, Wind Advisory, Flood Watch, Heat Advisory'],
                ['Minor', "Good to know. Probably won't hurt anyone.", 'Special Weather Statement, Air Quality Alert'],
              ]}
            />

            <SectionHeader>When Should I Act? (Urgency)</SectionHeader>
            <RefTable
              headers={['Urgency', 'What It Means']}
              rows={[
                ['Immediate', 'Do something NOW'],
                ['Expected', 'Do something within the hour'],
                ['Future', 'Coming in the next several hours'],
                ["Past", "It's over — NWS is clearing the alert"],
              ]}
            />

            <SectionHeader>How Sure Are They? (Certainty)</SectionHeader>
            <RefTable
              headers={['Certainty', 'What It Means']}
              rows={[
                ['Observed', "It's happening right now. Verified."],
                ['Likely', 'More than 50% chance'],
                ['Possible', 'Could happen, but less than 50%'],
                ["Unlikely", "Probably won't, but mentioned for awareness"],
              ]}
            />

            <SectionHeader>These Are Separate Scales</SectionHeader>
            <p>
              A single alert has all three. A hurricane warning for next week is "Severe + Future + Likely." A tornado spotted on the ground is "Extreme + Immediate + Observed." An air quality advisory is "Minor + Expected + Possible."
            </p>

            <SectionHeader>What Minimum Severity Should I Set?</SectionHeader>
            <RefTable
              headers={['Setting', 'What You Get', 'What You Miss']}
              rows={[
                ['Minor', 'Everything — high volume', 'Nothing'],
                [<><strong>Moderate</strong> ✓</>, 'Watches, Advisories, and Warnings', 'Special Weather Statements'],
                ['Severe', 'Only Warnings — things happening NOW', 'Watches (which give you hours of advance warning)'],
                ['Extreme', 'Only the rarest events', 'Most Tornado and Severe Thunderstorm Warnings'],
              ]}
            />
            <p>
              <strong>Moderate is recommended.</strong> It catches Watches (advance warning that conditions may worsen) and Advisories (conditions exist but aren't severe) while filtering out the informational stuff.
            </p>

            <SectionHeader>Finding Your NWS Zone</SectionHeader>
            <ol className="list-decimal list-inside ml-4 space-y-1">
              <li>Go to <ExtLink href="https://www.weather.gov">weather.gov</ExtLink></li>
              <li>Enter your location</li>
              <li>Find your zone code at <ExtLink href="https://www.weather.gov/pimar/PubZone">NWS Zone Map</ExtLink></li>
              <li>Zone codes look like: <Mono>IDZ016</Mono>, <Mono>UTZ040</Mono>, etc.</li>
            </ol>

            <SectionHeader>The User-Agent Field</SectionHeader>
            <p>
              NWS wants to know who's using their API — not for approval, just so they can contact you if something breaks. You make it up:
            </p>
            <p><Mono>(meshai, you@email.com)</Mono></p>
            <p>No registration. No waiting. Just type it in.</p>

            <SectionHeader>Learn More</SectionHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><ExtLink href="https://alerts.weather.gov">NWS Active Alerts</ExtLink> — see current alerts</li>
              <li><ExtLink href="https://www.weather.gov/documentation/services-web-api">NWS API Docs</ExtLink> — technical details</li>
            </ul>
          </TopicSection>

          {/* Solar & Geomagnetic */}
          <TopicSection id="solar" title="Solar & Geomagnetic Conditions">
            <SectionHeader>What You're Looking At</SectionHeader>
            <p>
              MeshAI tracks space weather — solar activity and its effects on Earth's magnetic field. This matters for radio operators because the sun directly controls how well HF radio works, and major solar events can affect all radio communications.
            </p>

            <SectionHeader>Solar Flux Index (SFI)</SectionHeader>
            <p>Think of SFI as a "how active is the sun" number. Higher = better for HF radio, but also higher risk of solar flares.</p>
            <RefTable
              headers={['SFI', 'What It Means for You']}
              rows={[
                ['Below 70', 'Quiet sun. Higher HF bands (10m, 15m) are probably dead. Stick to lower bands.'],
                ['70-90', 'Getting better. Some openings on 15m and above, but inconsistent.'],
                ['90-120', 'Good. Most HF bands work. Reliable contacts on 20m and 15m.'],
                ['120-170', 'Great. All HF bands open. 10m works for worldwide contacts.'],
                ['Above 170', 'Excellent. Best HF conditions — but watch for flares.'],
              ]}
            />
            <p><strong>Quick rule</strong>: SFI above 90 and Kp below 4 = good day for HF radio.</p>

            <SectionHeader>Kp Index</SectionHeader>
            <p>Kp measures how disturbed Earth's magnetic field is, on a 0-9 scale. Higher = more disturbance = worse for HF radio but better for aurora viewing.</p>
            <RefTable
              headers={['Kp', 'What It Means for You']}
              rows={[
                ['0-2', 'Quiet. Best HF conditions.'],
                ['3', "Slightly unsettled. You probably won't notice."],
                ['4', "Active. Some noise and fading on HF, especially if you're at higher latitudes."],
                [<strong>5</strong>, <><strong>Minor storm (G1).</strong> HF noticeably degraded. Aurora visible at high latitudes (~60°N).</>],
                [<strong>6</strong>, <><strong>Moderate storm (G2).</strong> HF getting rough. Aurora moving south (~55°N).</>],
                [<strong>7</strong>, <><strong>Strong storm (G3).</strong> HF unreliable for 1-2 days. Aurora at mid-latitudes.</>],
                [<strong>8-9</strong>, <><strong>Severe/Extreme storm.</strong> HF may black out completely. Aurora visible at very low latitudes. Power grid stress possible.</>],
              ]}
            />

            <SectionHeader>R / S / G Scales</SectionHeader>
            <p>NOAA's shorthand for three types of space weather events:</p>
            <SubHeader>R (Radio Blackouts) — from solar flares:</SubHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li>R1-R2: Brief HF disruption. You might not notice.</li>
              <li>R3: HF goes out for about an hour on the sunlit side of Earth.</li>
              <li>R4-R5: HF dead for hours. Serious.</li>
            </ul>
            <SubHeader>S (Solar Radiation Storms) — from energetic particles:</SubHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li>Mostly affects polar regions and satellites</li>
              <li>S3+: Polar HF goes out entirely</li>
            </ul>
            <SubHeader>G (Geomagnetic Storms) — from solar wind disturbances:</SubHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li>Same as the Kp scale: G1 = Kp 5, up to G5 = Kp 9</li>
            </ul>

            <SectionHeader>Bz — The Storm Predictor</SectionHeader>
            <p>
              Bz measures the direction of the solar wind's magnetic field. When it points south (negative values), the solar wind can dump energy into Earth's magnetic field, causing storms.
            </p>
            <RefTable
              headers={['Bz', 'What It Means']}
              rows={[
                ['Positive', 'All good. Solar wind bouncing off.'],
                ['0 to -5', 'Slight coupling. Nothing dramatic.'],
                ['-5 to -10', 'Things starting to pick up. Storm possible.'],
                ['Below -10', 'Storm likely. Kp will start climbing.'],
                ['Below -20', 'Severe storm probable.'],
              ]}
            />
            <p>Bz can change fast — minute to minute. What matters is whether it stays negative for hours, not brief dips.</p>

            <SectionHeader>Learn More</SectionHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><ExtLink href="https://www.swpc.noaa.gov">SWPC Space Weather Dashboard</ExtLink> — live data</li>
              <li><ExtLink href="https://www.swpc.noaa.gov/noaa-scales-explanation">NOAA Space Weather Scales</ExtLink> — what R/S/G mean</li>
              <li><ExtLink href="https://www.hamqsl.com/solar.html">HamQSL Solar Page</ExtLink> — ham-friendly display</li>
              <li><ExtLink href="https://www.swpc.noaa.gov/products/planetary-k-index">Planetary K-Index</ExtLink> — live Kp</li>
            </ul>
          </TopicSection>

          {/* Tropospheric Ducting */}
          <TopicSection id="ducting" title="Tropospheric Ducting">
            <SectionHeader>What You're Looking At</SectionHeader>
            <p>
              Sometimes the atmosphere creates an invisible "pipe" that traps radio signals and carries them much farther than normal. This is called tropospheric ducting. It mostly affects VHF and UHF frequencies.
            </p>
            <p>
              MeshAI watches for these conditions by analyzing weather data (temperature and humidity at different altitudes) over your mesh area.
            </p>

            <SectionHeader>How Do I Know If Ducting Is Happening?</SectionHeader>
            <p>MeshAI reports a "condition" based on the atmospheric profile:</p>
            <RefTable
              headers={['Condition', 'What It Means']}
              rows={[
                ['Normal', 'Standard propagation. Nothing unusual.'],
                ['Super-refraction', 'Slightly enhanced range. You might hear a few more distant stations than usual.'],
                ['Surface Duct', "Radio signals trapped near the ground. You may hear stations hundreds of km away that you've never heard before."],
                ['Elevated Duct', 'Same effect but the "pipe" is up in the atmosphere. Affects signals passing through that altitude.'],
              ]}
            />

            <SectionHeader>What You'll Actually Notice</SectionHeader>
            <p>When ducting happens on your mesh:</p>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li>Distant repeaters you've never heard suddenly come in</li>
              <li>Nodes appear from far outside your normal range</li>
              <li>You hear FM radio stations from other cities</li>
              <li>ADS-B flight tracking range gets much longer</li>
              <li>There might be interference from distant stations on your frequency</li>
            </ul>

            <SectionHeader>The dM/dz Number</SectionHeader>
            <p>The dashboard shows a "dM/dz" value in "M-units/km." You don't need to understand the math — just know:</p>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><strong>Around 118</strong> = normal atmosphere</li>
              <li><strong>Below 79</strong> = enhanced propagation starting</li>
              <li><strong>Below 0 (negative)</strong> = ducting is happening</li>
              <li><strong>Below -50</strong> = strong ducting — classic VHF/UHF DX event</li>
            </ul>

            <SectionHeader>When Does Ducting Happen?</SectionHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li>Under high-pressure weather systems (clear, stable air)</li>
              <li>When warm air sits on top of cool air (temperature inversion)</li>
              <li>Most common in late summer and early fall</li>
              <li>Strongest along coastlines and over water</li>
              <li>In mountain valleys: cold air pooling in fall/winter can create surface ducts</li>
            </ul>

            <SectionHeader>Setting It Up</SectionHeader>
            <p>
              Just configure the latitude and longitude of the center of your mesh area in Config → Environmental → Ducting. MeshAI checks the atmospheric conditions there every 3 hours using free weather model data. No API key needed.
            </p>

            <SectionHeader>Learn More</SectionHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><ExtLink href="https://dxinfocentre.com/tropo.html">Tropo Forecast Maps (Hepburn)</ExtLink> — 6-day tropo prediction</li>
              <li><ExtLink href="https://dxmaps.com">DX Maps</ExtLink> — real-time VHF/UHF propagation reports</li>
              <li><ExtLink href="https://en.wikipedia.org/wiki/Tropospheric_propagation">Wikipedia: Tropospheric Propagation</ExtLink> — background</li>
            </ul>
          </TopicSection>

          {/* Avalanche Danger */}
          <TopicSection id="avalanche" title="Avalanche Danger">
            <SectionHeader>What You're Looking At</SectionHeader>
            <p>
              MeshAI pulls avalanche forecasts from your regional avalanche center during winter months. The danger scale has 5 levels and it's the same across all of North America.
            </p>

            <SectionHeader>The Danger Scale</SectionHeader>
            <RefTable
              headers={['Level', 'Name', 'Color', 'What To Do']}
              rows={[
                ['1', 'Low', <StatusDot color="green" />, 'Generally safe. Normal caution in steep terrain.'],
                ['2', 'Moderate', <StatusDot color="yellow" />, 'Be careful on specific terrain features. Evaluate conditions.'],
                ['3', 'Considerable', <StatusDot color="orange" />, <><strong>DANGEROUS.</strong> This is where most people die in avalanches — they see "3 out of 5" and think it's fine. It's not. Use extreme caution.</>],
                ['4', 'High', <StatusDot color="red" />, <><strong>Very dangerous.</strong> Stay off anything steep.</>],
                ['5', 'Extreme', <StatusDot color="black" />, <><strong>Don't go out.</strong> Avalanches are happening on their own.</>],
              ]}
            />

            <SectionHeader>The Most Important Thing to Know</SectionHeader>
            <p>
              <strong>Level 3 (Considerable) kills more people than any other level.</strong> People look at "3 out of 5" and think "middle of the road, probably okay." In reality, the risk roughly doubles at each step up the scale. Level 3 is where dangerous conditions overlap with people thinking they can handle it.
            </p>

            <SectionHeader>Seasonal</SectionHeader>
            <p>
              MeshAI only checks avalanche conditions during winter months (configurable, default December through April). Outside season, it shows "off season" and saves API calls.
            </p>

            <SectionHeader>Finding Your Avalanche Center</SectionHeader>
            <p>
              Go to <ExtLink href="https://avalanche.org/avalanche-centers/">avalanche.org/avalanche-centers/</ExtLink> for a map. Common center codes:
            </p>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><Mono>SNFAC</Mono> — Sawtooth (central Idaho)</li>
              <li><Mono>UAC</Mono> — Utah</li>
              <li><Mono>NWAC</Mono> — Cascades/Olympics (WA/OR)</li>
              <li><Mono>CAIC</Mono> — Colorado</li>
              <li><Mono>SAC</Mono> — Sierra Nevada (CA)</li>
              <li><Mono>GNFAC</Mono> — Gallatin (SW Montana)</li>
            </ul>

            <SectionHeader>Learn More</SectionHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><ExtLink href="https://avalanche.org">Avalanche.org</ExtLink> — US forecasts</li>
              <li><ExtLink href="https://avalanche.org/avalanche-encyclopedia/human/resources/north-american-public-avalanche-danger-scale/">Avalanche Danger Scale</ExtLink> — full scale explanation</li>
              <li><ExtLink href="https://kbyg.org">Know Before You Go</ExtLink> — avalanche awareness</li>
            </ul>
          </TopicSection>

          {/* Traffic Flow */}
          <TopicSection id="traffic" title="Traffic Flow">
            <SectionHeader>What You're Looking At</SectionHeader>
            <p>
              MeshAI monitors traffic speed on road segments you configure, using data from TomTom (real vehicles with navigation apps reporting their speed).
            </p>

            <SectionHeader>Speed Ratio — The Key Number</SectionHeader>
            <p>MeshAI compares current speed to "free-flow speed" (what traffic normally does when the road is empty). The ratio tells you how congested it is:</p>
            <RefTable
              headers={['Ratio', 'What It Means']}
              rows={[
                [<><StatusDot color="green" /> Above 85%</>, 'Normal. Traffic flowing fine.'],
                [<><StatusDot color="yellow" /> 65-85%</>, 'Slow. Heavier than usual but moving.'],
                [<><StatusDot color="orange" /> 40-65%</>, 'Congested. Significant delays.'],
                [<><StatusDot color="red" /> Below 40%</>, 'Gridlock. Barely moving.'],
              ]}
            />
            <p>
              <strong>Note</strong>: "free-flow speed" is NOT the speed limit. It's what traffic actually does on that road when nobody's in the way. Drivers often exceed speed limits on open highways.
            </p>

            <SectionHeader>Confidence — Can You Trust the Data?</SectionHeader>
            <p>TomTom's confidence score tells you how much of the reading comes from real vehicles right now vs historical averages:</p>
            <RefTable
              headers={['Confidence', 'What It Means']}
              rows={[
                ['Above 0.9', 'Very reliable — lots of real-time probe data'],
                ['0.7-0.9', 'Good — mix of real-time and historical'],
                ['Below 0.7', <><strong>Unreliable</strong> — mostly guessing from historical patterns. Don't alert on this.</>],
              ]}
            />
            <p>Set minimum confidence to 0.7 to avoid false congestion alerts at night or on rural roads where few probe vehicles drive.</p>

            <SectionHeader>Setting Up Corridors</SectionHeader>
            <p>Each "corridor" is a point on a road you want to monitor. To add one:</p>
            <ol className="list-decimal list-inside ml-4 space-y-1">
              <li>Go to Google Maps, find the road</li>
              <li>Right-click the road → "What's here?" → copy the coordinates</li>
              <li>Add the corridor in Config with a name and those coordinates</li>
              <li>TomTom finds the nearest road segment automatically</li>
            </ol>

            <SectionHeader>Getting an API Key</SectionHeader>
            <ol className="list-decimal list-inside ml-4 space-y-1">
              <li>Sign up at <ExtLink href="https://developer.tomtom.com">developer.tomtom.com</ExtLink> (free)</li>
              <li>Create an app → get your API key</li>
              <li>Free tier: 2,500 requests/day (plenty for 5-10 corridors)</li>
            </ol>

            <SectionHeader>Learn More</SectionHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><ExtLink href="https://developer.tomtom.com">TomTom Developer Portal</ExtLink> — API docs and key signup</li>
              <li><ExtLink href="https://www.tomtom.com/traffic-index/">TomTom Traffic Index</ExtLink> — city congestion rankings</li>
            </ul>
          </TopicSection>

          {/* 511 Road Conditions */}
          <TopicSection id="roads-511" title="Road Conditions (511)">
            <SectionHeader>What You're Looking At</SectionHeader>
            <p>
              511 systems report road closures, construction, weather events, mountain pass conditions, and incidents. Every state runs their own 511 system — there is no national API.
            </p>

            <SectionHeader>Setting It Up</SectionHeader>
            <p>
              You need to find YOUR state's 511 developer API. MeshAI does not include a default URL because every state is different. Some states have free public APIs, some require registration, and some don't have developer APIs at all.
            </p>
            <p>Configure in Config → Environmental → 511:</p>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><strong>Base URL</strong> — your state's API endpoint</li>
              <li><strong>API Key</strong> — if required by your state</li>
              <li><strong>Endpoints</strong> — which data feeds to poll (varies by state)</li>
            </ul>

            <SectionHeader>Learn More</SectionHeader>
            <p>Check your state's 511 or DOT website for developer information.</p>
          </TopicSection>

          {/* Mesh Health */}
          <TopicSection id="mesh-health" title="Mesh Health">
            <SectionHeader>Health Score</SectionHeader>
            <p>MeshAI computes a 0-100 health score for your mesh network by looking at five areas, each weighted differently:</p>
            <RefTable
              headers={['Pillar', 'Weight', 'What It Measures']}
              rows={[
                [<strong>Infrastructure</strong>, '30%', 'Are your routers online?'],
                [<strong>Utilization</strong>, '25%', 'Is the radio channel congested?'],
                [<strong>Coverage</strong>, '20%', 'Do nodes have redundant paths to gateways?'],
                [<strong>Behavior</strong>, '15%', 'Are any nodes flooding the channel?'],
                [<strong>Power</strong>, '10%', 'Are battery-powered nodes running low?'],
              ]}
            />
            <p>The overall score is the weighted sum:</p>
            <p className="p-3 bg-slate-800 rounded font-mono text-sm">
              Score = (Infrastructure × 30%) + (Utilization × 25%) + (Coverage × 20%) + (Behavior × 15%) + (Power × 10%)
            </p>

            <SectionHeader>How Each Pillar Is Calculated</SectionHeader>

            <SubHeader>Infrastructure (30%)</SubHeader>
            <p>
              This is the simplest pillar — what percentage of your infrastructure nodes are currently online?
            </p>
            <p className="p-3 bg-slate-800 rounded font-mono text-sm">
              (routers online ÷ total routers) × 100
            </p>
            <p>
              Only nodes with the <Mono>ROUTER</Mono>, <Mono>ROUTER_LATE</Mono>, or <Mono>ROUTER_CLIENT</Mono> role count as infrastructure. Regular client nodes going offline doesn't affect this score. If you have 5 routers and 3 are online, infrastructure scores 60.
            </p>
            <p>
              <strong>Special case:</strong> If you have no routers at all (all clients), this pillar scores 100. You're not penalized for not having infrastructure — you just don't have any to track.
            </p>

            <SubHeader>Utilization (25%)</SubHeader>
            <p>
              MeshAI reads the channel utilization that each router reports in its telemetry — this is the firmware's own measurement of how busy the radio channel is. MeshAI uses the <strong>highest</strong> value from any infrastructure node because the busiest router is the bottleneck for the whole mesh.
            </p>
            <p>
              <strong>How it works:</strong>
            </p>
            <ol className="list-decimal list-inside space-y-1 ml-4">
              <li>Collect <Mono>channel_utilization</Mono> from all infrastructure nodes that report it</li>
              <li>If no infra nodes have telemetry, try all nodes</li>
              <li>Use the <strong>maximum</strong> value for scoring (busiest node = bottleneck)</li>
              <li>If no nodes report utilization (older firmware), fall back to packet count estimate</li>
            </ol>
            <p className="mt-4">
              <strong>Fallback method</strong> (when telemetry unavailable): estimates from packet counts using 200ms/packet airtime. This is less accurate — it assumes MediumFast preset and sums packets across all nodes.
            </p>
            <RefTable
              headers={['Channel Utilization', 'Score', 'What It Means']}
              rows={[
                ['Under 20%', '100', 'Channel is clear — this is the goal'],
                ['20-25%', '75-100', 'Slight degradation, occasional collisions'],
                ['25-35%', '50-75', 'Severe degradation — firmware throttling active'],
                ['35-45%', '25-50', 'Mesh struggling badly — reliability dropping'],
                ['Over 45%', '0-25', 'Mesh is effectively unusable'],
              ]}
            />
            <p>
              <strong>Special case:</strong> If no utilization data is available (no telemetry and no packet data), this pillar scores 100. You're not penalized for missing data.
            </p>

            <SubHeader>Coverage (20%)</SubHeader>
            <p>
              Measures gateway redundancy — how many of your data sources can "see" each node. A node reported by all 3 of your gateways has full coverage. A node only seen by 1 gateway is a single point of failure.
            </p>
            <p className="p-3 bg-slate-800 rounded font-mono text-sm">
              coverage_ratio = average_gateways_per_node ÷ total_sources<br/>
              single_gw_penalty = (single_gateway_nodes ÷ total_nodes) × 40
            </p>
            <p>
              If a node is seen by 2 out of 3 sources, its coverage ratio is 0.67. Infrastructure nodes with only single-gateway coverage get an extra penalty — they're critical but have no backup path.
            </p>
            <RefTable
              headers={['Coverage Ratio', 'Base Score', 'After Penalty']}
              rows={[
                ['100% (all sources)', '100', '100 minus single-gw penalty'],
                ['70-99%', '90', 'Minus penalties'],
                ['50-69%', '70', 'Minus penalties'],
                ['Under 50%', '50 or less', 'Heavy penalty'],
              ]}
            />
            <p>
              <strong>Special case:</strong> With only 1 data source, this pillar can't score well — there's no redundancy to measure. Coverage becomes meaningful when you have 2+ sources (MeshMonitor + MQTT, multiple gateways, etc.).
            </p>

            <SubHeader>Behavior (15%)</SubHeader>
            <p>
              Counts how many nodes are sending an unusually high number of non-text packets. This catches firmware bugs, stuck transmitters, and misconfigured nodes that are flooding the channel.
            </p>
            <p>
              <strong>What counts as flooding:</strong> More than 500 non-text packets in 24 hours. Text messages don't count — the behavior pillar only flags telemetry, position, and routing packet floods.
            </p>
            <RefTable
              headers={['Flagged Nodes', 'Score']}
              rows={[
                ['0', '100'],
                ['1', '80'],
                ['2-3', '60'],
                ['4-5', '40'],
                ['6+', '20'],
              ]}
            />
            <p>
              A single misbehaving node only drops the score to 80. It takes multiple problem nodes to seriously hurt the behavior pillar.
            </p>

            <SubHeader>Power (10%)</SubHeader>
            <p>
              Measures what fraction of battery-powered nodes are below the warning threshold (default 20%).
            </p>
            <p className="p-3 bg-slate-800 rounded font-mono text-sm">
              100 × (1 − low_battery_nodes ÷ total_battery_nodes)
            </p>
            <p>
              If 2 out of 10 battery nodes are below 20%, power scores 80.
            </p>
            <p>
              <strong>Important:</strong> USB-powered nodes are excluded from this calculation. Many nodes report 100% battery even when running on wall power with no battery installed. Only nodes actually running on batteries affect this pillar.
            </p>

            <SectionHeader>Health Tiers</SectionHeader>
            <RefTable
              headers={['Score', 'Tier', 'What It Means']}
              rows={[
                ['90-100', <><StatusDot color="green" /> Healthy</>, "Everything's working well."],
                ['75-89', <><StatusDot color="yellow" /> Slight degradation</>, 'Some issues but the mesh is functional.'],
                ['50-74', <><StatusDot color="orange" /> Unhealthy</>, 'Multiple problems. Reliability is affected.'],
                ['25-49', <><StatusDot color="red" /> Warning</>, 'Significant issues. The mesh is struggling.'],
                ['0-24', <><StatusDot color="black" /> Critical</>, 'Major failures. Barely functional.'],
              ]}
            />

            <SectionHeader>Channel Utilization — Is the Radio Channel Full?</SectionHeader>
            <p>
              Meshtastic radios share one LoRa channel. If too many nodes are transmitting too often, they step on each other and messages get lost.
            </p>
            <RefTable
              headers={['Utilization', "What's Happening"]}
              rows={[
                [<><StatusDot color="green" /> Under 25%</>, 'Healthy. The firmware itself starts throttling above 25% to protect the channel — so under 25% is the target.'],
                [<><StatusDot color="yellow" /> 25-40%</>, 'Getting busy. Common on larger meshes. Worth watching.'],
                [<><StatusDot color="orange" /> 40-50%</>, 'Congested. The firmware throttles GPS updates above 40%. Messages are colliding and retrying.'],
                [<><StatusDot color="red" /> Over 50%</>, 'Serious problem. More time is spent retrying than communicating. Mesh reliability drops fast.'],
                [<><StatusDot color="black" /> Over 65%</>, 'Documented failure point on busy LONG_FAST meshes. The mesh becomes unusable.'],
              ]}
            />

            <SectionHeader>Packet Flooding</SectionHeader>
            <p className="p-3 bg-yellow-500/10 border border-yellow-500/30 rounded text-yellow-200">
              <strong>⚠️ "Packet flooding" means a node sending too many RADIO PACKETS. This has nothing to do with water flooding.</strong>
            </p>
            <p>
              A normal Meshtastic node sends a packet every few minutes (announcing itself, reporting telemetry, updating position). If a node starts blasting packets every few seconds, something is wrong — firmware bug, stuck transmitter, or misconfiguration.
            </p>
            <RefTable
              headers={['Packets per Minute', 'What It Means']}
              rows={[
                ['1-5', 'Normal'],
                ['5-10', 'Elevated — might be someone chatting a lot'],
                ['10-20', 'Suspicious — worth investigating'],
                ['Over 30', 'Something is broken. This node is actively hurting the mesh.'],
              ]}
            />

            <SectionHeader>Battery Levels</SectionHeader>
            <p>
              Most Meshtastic radios (T-Beam, RAK4631, Heltec V3) use a single lithium battery cell. The voltage tells you how much charge is left:
            </p>
            <RefTable
              headers={['Voltage', 'Charge', 'What To Do']}
              rows={[
                ['4.20V', '100%', 'Full'],
                ['3.80V', '~60%', 'Fine'],
                [<strong>3.60V</strong>, <strong>~30%</strong>, <><strong>⚠️ Warning — charge it soon</strong></>],
                [<strong>3.50V</strong>, <strong>~15%</strong>, <><strong>🔴 Low — charge it now</strong></>],
                [<strong>3.40V</strong>, <strong>~7%</strong>, <><strong>⚫ About to die</strong></>],
                ['3.30V', '~3%', 'Device shutting down'],
              ]}
            />
            <p>
              <strong>USB-powered nodes</strong> report 100% battery even if there's no battery installed. Battery alerts only matter for nodes actually running on battery power.
            </p>

            <SectionHeader>Node Offline Detection</SectionHeader>
            <p>
              MeshAI marks a node as "offline" when it hasn't been heard for a configurable time period. Different node types need different thresholds:
            </p>
            <RefTable
              headers={['Node Type', 'Recommended Threshold', 'Why']}
              rows={[
                ['Fixed infrastructure (wall power)', <strong>2 hours</strong>, 'These should always be transmitting. 2 hours of silence means something is wrong.'],
                ['Fixed client (wall power)', '2-4 hours', 'Same logic, slightly more lenient.'],
                ['Mobile / vehicle', '4-8 hours', 'They go behind mountains, into garages, out of range. Normal.'],
                ['Solar-powered', '12-24 hours', 'May shut down at night when solar stops charging.'],
              ]}
            />
            <p>
              <strong>Rule of thumb</strong>: set the threshold to about 4× the node's beacon interval. Too tight and nodes will constantly flap "offline/online" from normal gaps. Too loose and real outages go unnoticed.
            </p>
          </TopicSection>

          {/* Broadcast Types (v0.6 schema split: New / Update / Active) */}
          <TopicSection id="broadcast-types" title="Broadcast Types">
            <p>
              Every broadcast the bot sends to the mesh carries a one-word prefix that
              tells you what kind of update it is. Three types:
            </p>
            <RefTable
              headers={['Prefix', 'What it means', 'When you see it']}
              rows={[
                [<Mono>New:</Mono>, "The first time the bot has ever broadcast about this event",
                  "Cache Peak Fire's WFIGS first-sight; FIRMS cluster's first 3-pixel detection; first NWS warning for a CAP id"],
                [<Mono>Update:</Mono>, "A material change on something the bot already announced",
                  "Cache Peak Fire's acreage grew; ITD 511 work zone's lane status changed; quake event's magnitude was revised"],
                [<Mono>Active:</Mono>, "A clock-driven reminder that an already-announced event is still live",
                  "Cache Peak Fire is still burning 8 hours later; an SWPC G3 storm is still in progress"],
              ]}
            />
            <p>
              The bot tracks first-broadcast time and last-broadcast time separately
              on every event row, so a New: prefix is only emitted once even after a
              container restart. Update: respects per-adapter cooldowns (WFIGS is 8 h
              by default; ITD 511 is per-incident). Active: is the reminder system,
              covered in the next section.
            </p>
          </TopicSection>

          {/* Reminder System (v0.6-phase3, clock-driven Active: re-broadcasts) */}
          <TopicSection id="reminders" title="Reminder System">
            <p>
              Some events stay live for days. A wildfire doesn't go out because
              WFIGS stopped publishing updates; a geomagnetic storm doesn't end
              because SWPC went quiet on the wire. The reminder system fires a
              clock-driven{' '}
              <Mono>Active:</Mono>-prefixed re-broadcast on a human-scale cadence so
              an operator who came on shift after the original announcement still
              sees the event.
            </p>

            <SectionHeader>Cadences</SectionHeader>
            <RefTable
              headers={['Adapter', 'Reminder cadence', 'Termination']}
              rows={[
                [<><Mono>wfigs</Mono> (wildfires)</>, 'Every 8 h while the fire is still active',
                  'WFIGS publishes a tombstone (incident closed) → fires.tombstoned_at is stamped → reminder loop stops'],
                [<><Mono>swpc</Mono> (space weather)</>, 'Every 8 h while a Kp >= floor / X-class flare / proton-storm event is ongoing',
                  'The next SWPC envelope shows the storm has subsided'],
                [<Mono>itd_511_work_zone</Mono>, 'Per-zone, configurable in the rule UI',
                  'WZDx publishes the zone with end_date in the past'],
              ]}
            />

            <SectionHeader>The tombstone</SectionHeader>
            <p>
              When a WFIGS update declares an incident closed, the bot stamps{' '}
              <Mono>fires.tombstoned_at</Mono> with the close time. The reminder
              scheduler treats <Mono>tombstoned_at IS NOT NULL</Mono> as "stop
              broadcasting Active: for this fire," and the LLM context layer treats
              it as "this fire is in the closed-out archive." A subsequent FIRMS
              pixel inside that fire's spread radius does not re-open it — closure
              is authoritative from NIFC.
            </p>

            <SectionHeader>Turning reminders off</SectionHeader>
            <p>
              Per-adapter on/off lives in <Mono>adapter_meta.reminder_enabled</Mono>{' '}
              and is exposed on the Adapter Config page. The reminders themselves
              flow through the same dispatcher gates as everything else, so they
              still respect cooldowns, the cold-start grace window, and your
              notification rules.
            </p>
          </TopicSection>

          {/* Notifications */}
          <TopicSection id="notifications" title="Notifications">
            <SectionHeader>How It Works</SectionHeader>
            <ol className="list-decimal list-inside ml-4 space-y-1">
              <li><strong>Something happens</strong> — a fire is detected, weather warning issued, node goes offline, etc.</li>
              <li><strong>MeshAI checks your rules</strong> — does this event match any of your notification rules? Is it severe enough?</li>
              <li><strong>If a rule matches</strong> — MeshAI sends the notification through whatever delivery method that rule is configured for.</li>
            </ol>

            <SectionHeader>Building Rules</SectionHeader>
            <p>Each rule answers three questions:</p>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><strong>WHEN</strong> does it trigger? (which categories, what severity)</li>
              <li><strong>WHERE</strong> does it send? (mesh broadcast, email, webhook, etc.)</li>
              <li><strong>HOW OFTEN</strong> at most? (cooldown period)</li>
            </ul>
            <p>
              Use "Add from Template" to start with a pre-built rule and customize it, or build from scratch with "Add Rule."
            </p>

            <SectionHeader>Severity Levels — What Should I Set?</SectionHeader>
            <RefTable
              headers={['Level', 'When It\'s Used', 'Notification Volume']}
              rows={[
                ['Info', 'Routine stuff (ducting detected, new router appeared)', 'High — lots of messages'],
                ['Advisory', 'Worth knowing (weather advisory, slow traffic, battery declining)', 'Moderate'],
                ['Watch', 'Pay attention (fire within 50km, weather watch, stream rising)', 'Low-moderate'],
                [<><strong>Warning</strong> ✓</>, 'Take action (fire within 15km, severe weather, critical battery)', 'Low — recommended for most rules'],
                ['Emergency', 'Life safety (extreme weather, fire at infrastructure, total blackout)', 'Very rare'],
              ]}
            />
            <p>
              <strong>"Warning" is the sweet spot for most rules.</strong> You get alerted when something actually needs your attention without being overwhelmed by every minor event.
            </p>

            <SectionHeader>Webhook — The Swiss Army Knife</SectionHeader>
            <p>
              A webhook sends your alert as an HTTP POST to any URL. This one delivery method works with:
            </p>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><strong>Discord</strong> — use a Discord webhook URL</li>
              <li><strong>Slack</strong> — use a Slack incoming webhook URL</li>
              <li><strong>ntfy.sh</strong> — POST to <Mono>https://ntfy.sh/your-topic</Mono></li>
              <li><strong>Pushover</strong> — POST to the Pushover API</li>
              <li><strong>Home Assistant</strong> — POST to an automation webhook URL</li>
              <li>Anything else that accepts HTTP POST</li>
            </ul>
            <p>
              MeshAI doesn't need to know what's on the other end. Give it the URL and it works.
            </p>
          </TopicSection>

          {/* Commands */}
          <TopicSection id="commands" title="Commands">
            <p>
              All commands use the <Mono>!</Mono> prefix (configurable). Send these as a direct message to MeshAI on your mesh.
            </p>

            <SectionHeader>Basic Commands</SectionHeader>
            <RefTable
              headers={['Command', 'What It Does']}
              rows={[
                [<Mono>!help</Mono>, 'Shows all available commands'],
                [<Mono>!ping</Mono>, 'Tests if the bot is alive'],
                [<Mono>!status</Mono>, 'Quick mesh summary (nodes online, health score)'],
                [<Mono>!health</Mono>, 'Detailed health report with pillar scores'],
                [<Mono>!weather</Mono>, 'Current weather for your area'],
              ]}
            />

            <SectionHeader>Environmental Commands</SectionHeader>
            <RefTable
              headers={['Command', 'What It Does']}
              rows={[
                [<Mono>!alerts</Mono>, 'Active NWS weather alerts for your area'],
                [<><Mono>!solar</Mono> (or <Mono>!hf</Mono>)</>, 'Current solar indices and RF conditions'],
                [<Mono>!fire</Mono>, 'Active wildfires near your mesh'],
                [<Mono>!avy</Mono>, 'Avalanche advisory (seasonal — shows "off season" in summer)'],
                [<><Mono>!streams</Mono> (or <Mono>!gauges</Mono>)</>, 'Stream gauge readings'],
                [<><Mono>!roads</Mono> (or <Mono>!traffic</Mono>)</>, 'Road conditions and traffic flow'],
                [<Mono>!hotspots</Mono>, 'Satellite fire detections'],
              ]}
            />

            <SectionHeader>Conversational</SectionHeader>
            <p>
              Bang commands are the short, predictable interface. For anything that
              doesn't map cleanly to a single command — "how's the mesh doing?",
              "is there any ducting?", "why didn\'t I hear about anything today?"
              — you can DM the bot in plain English. The LLM DM path covers the
              same data the commands cover, plus the dispatcher drop audit, with
              honest "no data" answers when a feed is quiet. Full catalog under{' '}
              <a href="#llm-dm" className="text-accent hover:underline">LLM DM
              Queries</a>.
            </p>
          </TopicSection>

          {/* LLM DM (Natural-Language Queries) — v0.7-fire-tracker-4 7-path */}
          <TopicSection id="llm-dm" title="LLM DM (Natural-Language Queries)">
            <p>
              Bang commands like <Mono>!fire</Mono> are short and predictable — the
              right tool on a mesh-constrained interface. For anything else, you can
              DM the bot in plain English and it will answer from the same live
              environmental data the broadcast pipeline uses. Both paths work; pick
              whichever fits the question.
            </p>

            <SectionHeader>What it can answer</SectionHeader>
            <p>
              When you DM the bot a question, the env_reporter layer assembles up to
              seven data blocks and injects them into the LLM's system prompt. Each
              block maps to one adapter:
            </p>
            <RefTable
              headers={["Adapter block", "Example question that hits it", "What you get back"]}
              rows={[
                [<Mono>build_fires_detail</Mono>,
                  '"are there any fires near me?"',
                  "Active WFIGS-declared fires, acreage, containment, declared_at, county/state"],
                [<Mono>build_alerts_detail</Mono>,
                  '"any weather alerts?"',
                  "Active NWS CAP alerts: type, severity, area, expiry"],
                [<Mono>build_quakes_detail</Mono>,
                  '"any earthquakes nearby?"',
                  "USGS quakes in the last 24h: magnitude, depth, place"],
                [<Mono>build_traffic_detail</Mono>,
                  '"how is traffic on I-84?" / "any road closures?"',
                  "TomTom + ITD 511 active incidents"],
                [<Mono>build_gauges_detail</Mono>,
                  '"what is the snake river level?"',
                  "USGS NWIS latest readings + flood stages"],
                [<Mono>build_swpc_detail</Mono>,
                  '"what are the band conditions?" / "any space weather?"',
                  "Recent SWPC events + band-conditions ratings"],
                [<Mono>build_drop_audit</Mono>,
                  `"why didn't I hear about anything today?"`,
                  "Event log: what envelopes the dispatcher filtered, by adapter + category"],
              ]}
            />

            <SectionHeader>The grounding rule</SectionHeader>
            <p>
              The bot is told to answer <em>only</em> from the blocks in the system
              prompt. If a block is empty (no recent quakes, no active NWS alerts),
              the response is honest about it: "No active weather alerts right now,"
              not a fabricated "144 earthquakes worldwide in the past 24 hours."
              That clamp closes the failure mode where the LLM defaulted to its
              training data when local tables were quiet.
            </p>

            <SectionHeader>Excluding an adapter from LLM context</SectionHeader>
            <p>
              The <Mono>include_in_llm_context</Mono> toggle on each adapter's row
              in Adapter Config decides whether that adapter's <Mono>build_*</Mono>{' '}
              block lands in the system prompt. Turn an adapter off here if you
              don't want the bot's natural-language answers to draw on it (e.g.
              you ingest TomTom for situational awareness but don't want it cited
              in DM answers). Broadcasts are unaffected — this toggle gates LLM
              context only.
            </p>

            <SectionHeader>What it can't answer</SectionHeader>
            <p>
              The bot has no general internet access. Questions that need data the
              env_reporter doesn't carry ("what's the weather forecast tomorrow",
              "who's the current president") fall back to whatever the configured
              LLM backend knows from training. The grounding clamp keeps the bot
              from inventing local data, but it can't keep the LLM from speculating
              about non-local topics.
            </p>
          </TopicSection>

          {/* OR-not-AND Architecture (Central vs native, mutually exclusive) */}
          <TopicSection id="or-not-and" title="OR-not-AND Architecture">
            <p>
              Every environmental adapter pulls its data from one of two places:
            </p>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li>
                <strong>Central</strong> (canonical) — Central polls the upstream
                feed once on behalf of the whole fleet and re-publishes normalized
                envelopes over NATS JetStream. MeshAI subscribes. One Central poll,
                one canonical normalization, many subscribers.
              </li>
              <li>
                <strong>Native</strong> — MeshAI polls the upstream feed directly.
                Stays around for adapters Central doesn't carry yet (currently
                Tropospheric Ducting and Avalanche Center advisories) and for
                operators who don't run Central.
              </li>
            </ul>

            <SectionHeader>Why mutually exclusive</SectionHeader>
            <p>
              An adapter is set to <strong>either</strong> Central <strong>or</strong>{' '}
              native, never both. Running both at the same time is what the
              codebase calls the <em>AND-mode anti-pattern</em>: two independent
              poll loops on the same upstream feed, duplicate broadcasts, duplicate
              cursor state, no shared dedup. The Spokane-class leak (cross-state
              broadcasts that escaped the bbox filter in May 2026) was caused by
              an inadvertent AND-mode on the traffic adapter; the fix made the
              gate enforce mutual exclusion at boot and on every config save.
            </p>

            <SectionHeader>The per-adapter source toggle</SectionHeader>
            <p>
              Set <Mono>feed_source</Mono> on each adapter's row in Environment:
            </p>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><Mono>central</Mono> — disable the native poll loop, subscribe to the matching Central subject pattern.</li>
              <li><Mono>native</Mono> — disable the Central subscription for this adapter, run the native poller.</li>
            </ul>
            <p>
              On the GUI, adapters with <em>no Central counterpart yet</em> show
              their Central button disabled with a "native only" tooltip. That's
              not an AND state; the adapter is still single-source, just locked to
              native by upstream availability.
            </p>

            <SectionHeader>Where this surfaces in tooltips</SectionHeader>
            <p>
              You'll see "AND-model anti-pattern" referenced in two places: the
              USGS-lookup button on Gauge Sites (disabled when the USGS adapter is
              on Central, because doing a one-off direct USGS poll from the GUI
              while the runtime is on Central is precisely the AND-mode this rule
              forbids) and the env_routes 404 response on{' '}
              <Mono>/api/env/usgs/lookup/{'{site_id}'}</Mono> in central-feed mode.
              Both surfaces refuse to fall back to a direct upstream call; the
              right answer is to enter values manually or source them from Central.
            </p>
          </TopicSection>

          {/* Adapter Config + CONFIG-vs-CODE Rule (the GUI knob hub) */}
          <TopicSection id="adapter-config" title="Adapter Config & the CODE Rule">
            <p>
              The Adapter Config page is the single hub for ~50 GUI-editable knobs
              across the 13 adapters that touch the broadcast pipeline. Changes
              take effect on the next handler call — no container restart needed
              for most keys.
            </p>

            <SectionHeader>The CONFIG-vs-CODE rule</SectionHeader>
            <p>
              Not everything tunable becomes a GUI row. The codebase splits along
              one rule:
            </p>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><strong>CONFIG</strong> (lives on this page) — where you send (channels), how often (cadences, schedules), thresholds (magnitude floors, severity gates, distance radii, cooldown durations, freshness windows), curation data (which sites, states, codes), toggles (enabled, include_in_llm_context).</li>
              <li><strong>CODE</strong> (stays in the handlers, not on the GUI) — sentence templates, emoji choices, mapping / translation functions (TomTom icon_map, ITD sub_type_map, Central adapter_map and category_map), rendering logic (anchor priority order, expires-buckets formatting, threshold-state labels), heuristic logic (band_conditions Kp/SFI → Good/Fair/Poor function).</li>
            </ul>
            <p>
              If you find yourself wanting to add a wire-string template or an
              emoji to the GUI, stop — that's CODE. If you want to change a
              threshold or a curation list, the GUI is the right place.
            </p>

            <SectionHeader>Restart-required vs live</SectionHeader>
            <p>
              Most keys take effect on the next handler call (the env_store re-reads
              from the database). A short list requires a container restart, because
              they govern startup-only wiring:
            </p>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li>Anything under the <Mono>environmental</Mono> section on the Config page (feed_source, central URL, etc.). The Spokane-fix gate runs at env_store boot and at CentralConsumer subscribe — both happen only at startup.</li>
              <li>The LLM backend swap (Google → Anthropic → OpenAI).</li>
              <li>The dispatcher cold-start grace window.</li>
            </ul>
            <p>
              When you save one of those keys via the GUI, a yellow Restart-Required
              banner surfaces at the top of the page with a "Restart now" button.
              Until you click it, the on-disk config and the running config
              intentionally disagree — that's the OR-not-AND gate refusing to
              transition mid-flight.
            </p>

            <SectionHeader>The <Mono>include_in_llm_context</Mono> toggle</SectionHeader>
            <p>
              Each adapter's card on Adapter Config carries a per-adapter
              "LLM context" switch. When off, that adapter's <Mono>build_*</Mono>{' '}
              env_reporter block is skipped during system-prompt assembly. Broadcasts
              are unaffected; this toggle is purely about what the LLM sees when
              you DM it. See the LLM DM section above for the seven adapter blocks
              this gates.
            </p>
          </TopicSection>

          {/* Curation Tables: Gauge Sites + Town Anchors */}
          <TopicSection id="curation" title="Curation: Gauge Sites & Town Anchors">
            <p>
              Two curation tables drive the broadcast text the bot puts on the mesh.
              Both are CRUD UIs with per-row enable/disable; both fall through to
              fallback chains when a row is missing or disabled.
            </p>

            <SectionHeader>Gauge Sites</SectionHeader>
            <p>
              Stream gauge thresholds for the USGS NWIS handler. Each row pairs a
              USGS site_id with a human gauge name, lat/lon, and four NWS-AHPS
              flood thresholds in feet: Action, Minor, Moderate, Major. The
              handler compares an incoming gauge reading to those thresholds and
              emits the right broadcast severity.
            </p>
            <p>
              <strong>USGS lookup button</strong> — when you add a new row in
              native-feed mode, the lookup queries the USGS Site Service plus NWS
              NWPS to auto-populate name, coordinates, and flood stages. In
              central-feed mode the button is disabled with a tooltip: a one-off
              direct USGS poll from the GUI while the runtime is on Central is the
              AND-mode anti-pattern the architecture forbids. Enter values
              manually or pull them from Central.
            </p>
            <p>
              <strong>Disabled rows</strong> are ignored at dispatch time. The
              corresponding gauge still ingests into <Mono>gauge_readings</Mono>{' '}
              (so historical queries still work), it just doesn't broadcast.
            </p>

            <SectionHeader>Town Anchors</SectionHeader>
            <p>
              Lookup table for the "X mi {'<'}bearing{'>'} of {'<'}town{'>'}" suffix
              in broadcast text. When a fire or NWS alert renders, the bot walks an
              anchor chain to figure out where to say it is:
            </p>
            <ol className="list-decimal list-inside ml-4 space-y-1">
              <li>Photon nearest-town lookup (the WFIGS path uses this — produces "near Long Creek Summit Home" style anchors)</li>
              <li>Town Anchors table (your curated list)</li>
              <li>Landclass label (county / federal-land identifier)</li>
              <li>County + state fallback</li>
              <li>Bare lat/lon coords</li>
            </ol>
            <p>
              Each row carries a name (lowercased on save), state, lat/lon, and an
              enable flag. The "lowercased on save" rule keeps "Almo" / "ALMO" /
              "almo" from being three distinct rows. Disabled rows fall through to
              the next anchor in the chain — the broadcast text still goes out, it
              just uses a different anchor.
            </p>
            <p>
              Example broadcast text rendered from a Town Anchors row:{' '}
              <span className="text-amber-300">"🔥 New: Cache Peak Fire (WF), 3 mi N of Almo: 250 ac, 0% contained, @ 42.118,-113.643"</span>
            </p>
          </TopicSection>

          {/* Schema Migrations (light touch — for ops + debugging) */}
          <TopicSection id="schema" title="Schema Migrations">
            <p>
              MeshAI persists state in a single SQLite database
              (<Mono>/data/meshai.sqlite</Mono>) with WAL journaling. Schema
              migrations live in <Mono>meshai/persistence/migrations/v*.sql</Mono>{' '}
              and apply automatically on container start. The runner reads the
              migrations directory, sorts by version, and applies anything past
              the current <Mono>schema_meta.version</Mono> in order. Idempotent
              re-runs are no-ops.
            </p>

            <SectionHeader>v0.6 + v0.7 additions</SectionHeader>
            <RefTable
              headers={['Migration', 'What it added']}
              rows={[
                [<Mono>v11</Mono>, 'first_broadcast_at + last_broadcast_at split + reminder_enabled per adapter (the schema basis for New / Update / Active)'],
                [<Mono>v12</Mono>, 'fires.tombstoned_at (WFIGS closure stamp; terminates the reminder loop)'],
                [<Mono>v13</Mono>, 'Fire Tracker Phase 1 — fire_pixels table + spread_radius_mi + current_centroid_lat/lon + last_hotspot_at; firms_pixels attributed_at + cluster_broadcast_at'],
                [<Mono>v14</Mono>, 'Fire Tracker Phase 2 — fire_passes table (per-satellite-pass centroid + drift) + last_pass_id + halt_broadcast_at on fires'],
                [<Mono>v15</Mono>, 'Fire Tracker Phase 3 — fire_passes.perimeter_geojson (convex hull) + fires.last_spotting_broadcast_at'],
                [<Mono>v16</Mono>, 'Fire Tracker Phase 4 — fire_digest_broadcasts table (idempotent twice-daily LLM digest)'],
              ]}
            />

            <SectionHeader>When migrations fail</SectionHeader>
            <p>
              A migration failure leaves the database at the prior version and
              raises in the runner. Container logs surface the SQL error;{' '}
              <Mono>schema_meta.version</Mono> tells you where the last
              successful migration stopped. Re-running the container after
              the underlying issue is fixed picks up from there.
            </p>
          </TopicSection>

          {/* API Reference */}
          <TopicSection id="api" title="API Reference">
            <p>
              MeshAI's REST API is available at <Mono>http://your-host:8080</Mono>. All endpoints return JSON.
            </p>

            <SectionHeader>System</SectionHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><Mono>GET /api/status</Mono> — version, uptime, node count</li>
              <li><Mono>GET /api/channels</Mono> — radio channel list</li>
              <li><Mono>POST /api/restart</Mono> — restart the bot</li>
            </ul>

            <SectionHeader>Mesh Data</SectionHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><Mono>GET /api/health</Mono> — health score and pillars</li>
              <li><Mono>GET /api/nodes</Mono> — all nodes with positions and telemetry</li>
              <li><Mono>GET /api/edges</Mono> — neighbor links with signal quality</li>
              <li><Mono>GET /api/regions</Mono> — region summaries</li>
              <li><Mono>GET /api/sources</Mono> — data source health</li>
            </ul>

            <SectionHeader>Configuration</SectionHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><Mono>GET /api/config</Mono> — full config</li>
              <li><Mono>GET /api/config/{'{section}'}</Mono> — one section</li>
              <li><Mono>PUT /api/config/{'{section}'}</Mono> — update a section</li>
            </ul>

            <SectionHeader>Environmental</SectionHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><Mono>GET /api/env/status</Mono> — per-feed health</li>
              <li><Mono>GET /api/env/active</Mono> — all active events</li>
              <li><Mono>GET /api/env/swpc</Mono> — solar/geomagnetic data</li>
              <li><Mono>GET /api/env/ducting</Mono> — atmospheric profile</li>
              <li><Mono>GET /api/env/fires</Mono> — wildfire perimeters</li>
              <li><Mono>GET /api/env/hotspots</Mono> — satellite fire detections</li>
            </ul>

            <SectionHeader>Alerts</SectionHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><Mono>GET /api/alerts/active</Mono> — current alerts</li>
              <li><Mono>GET /api/alerts/history</Mono> — past alerts</li>
              <li><Mono>GET /api/notifications/categories</Mono> — available alert categories</li>
            </ul>

            <SectionHeader>Real-time</SectionHeader>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><Mono>ws://your-host:8080/ws/live</Mono> — WebSocket for live updates</li>
            </ul>
          </TopicSection>

        </div>
      </div>
    </div>
  )
}
