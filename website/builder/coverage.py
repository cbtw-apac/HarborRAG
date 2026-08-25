"""CoverageBuildMixin implementation for the website builder."""

from pathlib import Path


class CoverageBuildMixin:
    """Focused website-build operations composed by ``WebsiteBuilder``."""

    def build_coverage_structure(self, coverage_dir: str | None = None) -> dict:
        """Build coverage report structure."""
        # Always create coverage output directory
        coverage_output_dir = self.output_dir / "coverage"
        coverage_output_dir.mkdir(parents=True, exist_ok=True)

        if not coverage_dir:
            return {"coverage_reports": []}

        coverage_path = Path(coverage_dir)
        if not coverage_path.exists():
            return {"coverage_reports": []}

        # Copy all coverage files with proper naming
        import shutil

        for item in coverage_path.iterdir():
            # Map directory names to cleaner package names
            dest_name = item.name
            if item.is_dir():
                if "htmlcov-loader" in item.name:
                    dest_name = "loader"
                elif "htmlcov-mcp" in item.name:
                    dest_name = "mcp"
                elif "htmlcov-website" in item.name:
                    dest_name = "website"
                # branding-compat: accept coverage artifacts produced before the rename.
                elif "htmlcov-core" in item.name or "htmlcov-qdrant-loader-core" in item.name:
                    dest_name = "core"
                elif "htmlcov" in item.name:
                    dest_name = item.name.replace("htmlcov-", "").replace("htmlcov_", "")

            dest_path = coverage_output_dir / dest_name
            try:
                if item.is_file():
                    shutil.copy2(item, dest_path)
                elif item.is_dir():
                    if dest_path.exists():
                        shutil.rmtree(dest_path)
                    shutil.copytree(item, dest_path)
                    print(f"📁 Copied coverage: {item.name} -> {dest_name}")
            except Exception as e:
                print(f"⚠️  Failed to copy coverage file {item}: {e}")

        # Build reports list using the renamed directories
        reports = []
        for subdir in coverage_output_dir.iterdir():
            if subdir.is_dir():
                index_file = subdir / "index.html"
                if index_file.exists():
                    reports.append(
                        {
                            "name": subdir.name,
                            "path": f"{subdir.name}/index.html",
                            "url": f"coverage/{subdir.name}/index.html",
                        }
                    )

        # Create main coverage index page using site template when reports exist
        if reports:
            # Build coverage index with Bootstrap styling
            index_content = """
<section class=\"py-5\">
  <div class=\"container\">
    <h1 class=\"display-5 fw-bold text-primary mb-4\"><i class=\"bi bi-graph-up me-2\"></i>Coverage Reports</h1>
    <div class=\"row g-4\">"""

            for report in reports:
                if report["name"] == "loader":
                    index_content += """
            <div class="col-lg-6">
                <div class="card">
                    <div class="card-header">
                        <h4>HarborRAG</h4>
                        <span id="loader-test-indicator" class="badge">Loading...</span>
                    </div>
                    <div class="card-body">
                        <div id="loader-coverage">HarborRAG coverage data</div>
                        <a href="loader/" class="btn btn-primary">View Detailed Report</a>
                    </div>
                </div>
            </div>"""
                elif report["name"] == "mcp":
                    index_content += """
            <div class="col-lg-6">
                <div class="card">
                    <div class="card-header">
                        <h4>MCP Server</h4>
                        <span id="mcp-test-indicator" class="badge">Loading...</span>
                    </div>
                    <div class="card-body">
                        <div id="mcp-coverage">MCP Server coverage data</div>
                        <a href="mcp/" class="btn btn-success">View Detailed Report</a>
                    </div>
                </div>
            </div>"""
                elif report["name"] == "website":
                    index_content += """
            <div class="col-lg-6">
                <div class="card">
                    <div class="card-header">
                        <h4>Website</h4>
                        <span id="website-test-indicator" class="badge">Loading...</span>
                    </div>
                    <div class="card-body">
                        <div id="website-coverage">Website coverage data</div>
                        <a href="website/" class="btn btn-info">View Detailed Report</a>
                    </div>
                </div>
            </div>"""
                elif report["name"] == "core":
                    index_content += """
            <div class="col-lg-6">
                <div class="card">
                    <div class="card-header">
                        <h4>Core Library</h4>
                        <span id="core-test-indicator" class="badge">Loading...</span>
                    </div>
                    <div class="card-body">
                        <div id="core-coverage">Core library coverage data</div>
                        <a href="core/" class="btn btn-warning">View Detailed Report</a>
                    </div>
                </div>
            </div>"""

            index_content += """
    </div>
  </div>
</section>

<script>
// Compute and render coverage summary from status.json
function coverageSummary(data){
  try{
    let total = 0, missing = 0;
    if (data && data.files){
      for (const k in data.files){
        const f = data.files[k];
        const nums = f && f.index && f.index.nums ? f.index.nums : (f.index && f.index.numbers ? f.index.numbers : null);
        if (nums && typeof nums.n_statements === 'number'){
          total += (nums.n_statements||0);
          missing += (nums.n_missing||0);
        }
      }
    }
    // Fallback if a totals object exists
    if (total === 0 && data && data.totals){
      if (typeof data.totals.n_statements === 'number'){
        total = data.totals.n_statements||0;
        missing = data.totals.n_missing||0;
      } else if (typeof data.totals.covered_lines === 'number' && typeof data.totals.num_statements === 'number'){
        total = data.totals.num_statements;
        missing = total - data.totals.covered_lines;
      }
    }
    if (total > 0){
      const covered = Math.max(0, total - missing);
      const pct = Math.round((covered/total)*1000)/10; // one decimal
      return {pct, covered, total};
    }
  } catch(e){}
  return null;
}

function renderCoverage(id, summary){
  const el = document.getElementById(id);
  if (!el) return;
  if (!summary){ el.textContent = 'Loaded'; return; }
  const {pct, covered, total} = summary;
  el.innerHTML = `
    <div class="d-flex align-items-center">
      <div class="progress flex-grow-1 me-2" style="height: 10px;">
        <div class="progress-bar bg-success" role="progressbar" style="width: ${pct}%" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"></div>
      </div>
      <span class="small fw-semibold">${pct}% (${covered}/${total})</span>
    </div>`;
}

fetch('loader/status.json').then(r=>r.json()).then(d=>renderCoverage('loader-coverage', coverageSummary(d))).catch(()=>{});
fetch('mcp/status.json').then(r=>r.json()).then(d=>renderCoverage('mcp-coverage', coverageSummary(d))).catch(()=>{});
fetch('website/status.json').then(r=>r.json()).then(d=>renderCoverage('website-coverage', coverageSummary(d))).catch(()=>{});
fetch('core/status.json').then(r=>r.json()).then(d=>renderCoverage('core-coverage', coverageSummary(d))).catch(()=>{});
</script>
"""
            # Render through site template for full styling/navigation
            self.build_page(
                "base.html",
                "coverage/index.html",
                "Coverage Reports",
                "Test coverage analysis",
                "coverage/index.html",
                content=index_content,
            )
            print("📄 Generated coverage index.html")

        return {"coverage_reports": reports}
