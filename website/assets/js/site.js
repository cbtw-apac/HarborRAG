(() => {
  "use strict";

  document.documentElement.classList.add("motion-ready");

  const selectKeyboardTab = (event, tabs, currentIndex, activate) => {
    const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
    if (!keys.includes(event.key)) return;
    event.preventDefault();

    let nextIndex = currentIndex;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = tabs.length - 1;
    if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length;
    activate(tabs[nextIndex], true);
  };

  document.querySelectorAll("[data-architecture-explorer]").forEach((explorer) => {
    const routes = Array.from(explorer.querySelectorAll("[data-architecture-route]"));
    const flows = Array.from(explorer.querySelectorAll("[data-architecture-flow]"));
    const nodes = Array.from(explorer.querySelectorAll("[data-architecture-node]"));
    const details = Array.from(explorer.querySelectorAll("[data-architecture-detail]"));

    const selectNode = (node) => {
      const name = node.dataset.architectureNode;
      nodes.forEach((candidate) => {
        const active = candidate === node;
        candidate.classList.toggle("is-active", active);
        candidate.setAttribute("aria-pressed", String(active));
      });
      details.forEach((detail) => {
        detail.hidden = detail.dataset.architectureDetail !== name;
      });
    };

    const selectRoute = (button, focusTab = false) => {
      const route = button.dataset.architectureRoute;
      routes.forEach((candidate) => {
        const active = candidate === button;
        candidate.classList.toggle("is-active", active);
        candidate.setAttribute("aria-selected", String(active));
        candidate.tabIndex = active ? 0 : -1;
      });
      flows.forEach((flow) => {
        flow.hidden = flow.dataset.architectureFlow !== route;
      });
      const selectedFlow = flows.find((flow) => flow.dataset.architectureFlow === route);
      const firstNode = selectedFlow?.querySelector("[data-architecture-node]");
      if (firstNode) selectNode(firstNode);
      if (focusTab) button.focus();
    };

    routes.forEach((button, index) => {
      button.addEventListener("click", () => selectRoute(button));
      button.addEventListener("keydown", (event) => {
        selectKeyboardTab(event, routes, index, selectRoute);
      });
    });

    nodes.forEach((node) => node.addEventListener("click", () => selectNode(node)));
  });

  document.querySelectorAll("[data-interface-examples]").forEach((examples) => {
    const tabs = Array.from(examples.querySelectorAll("[data-interface-tab]"));
    const panels = Array.from(examples.querySelectorAll("[data-interface-panel]"));

    const selectInterface = (button, focusTab = false) => {
      const selected = button.dataset.interfaceTab;
      tabs.forEach((candidate) => {
        const active = candidate === button;
        candidate.classList.toggle("active", active);
        candidate.setAttribute("aria-selected", String(active));
        candidate.tabIndex = active ? 0 : -1;
      });
      panels.forEach((panel) => {
        panel.hidden = panel.dataset.interfacePanel !== selected;
      });
      if (focusTab) button.focus();
    };

    tabs.forEach((button, index) => {
      button.addEventListener("click", () => selectInterface(button));
      button.addEventListener("keydown", (event) => {
        selectKeyboardTab(event, tabs, index, selectInterface);
      });
    });
  });

  const navToggle = document.querySelector("[data-nav-toggle]");
  const siteNav = document.querySelector("[data-site-nav]");
  if (navToggle && siteNav) {
    const closeNavigation = () => {
      navToggle.setAttribute("aria-expanded", "false");
      siteNav.classList.remove("is-open");
    };
    navToggle.addEventListener("click", () => {
      const open = navToggle.getAttribute("aria-expanded") !== "true";
      navToggle.setAttribute("aria-expanded", String(open));
      siteNav.classList.toggle("is-open", open);
    });
    siteNav.querySelectorAll("a").forEach((link) => {
      link.addEventListener("click", closeNavigation);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && navToggle.getAttribute("aria-expanded") === "true") {
        closeNavigation();
        navToggle.focus();
      }
    });
  }

  const revealItems = Array.from(document.querySelectorAll("[data-reveal]"));
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduceMotion || !("IntersectionObserver" in window)) {
    revealItems.forEach((item) => item.classList.add("is-visible"));
  } else {
    const revealObserver = new IntersectionObserver(
      (entries, observer) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        });
      },
      { rootMargin: "0px 0px -8%", threshold: 0.08 }
    );
    revealItems.forEach((item) => revealObserver.observe(item));
  }

  const header = document.querySelector("[data-site-header]");
  const progress = document.querySelector("[data-page-progress]");
  if (header || progress) {
    let scheduled = false;
    const updateScrollState = () => {
      const scrollTop = window.scrollY;
      if (header) header.classList.toggle("is-scrolled", scrollTop > 12);
      if (progress) {
        const available = document.documentElement.scrollHeight - window.innerHeight;
        const ratio = available > 0 ? Math.min(scrollTop / available, 1) : 0;
        progress.style.transform = `scaleX(${ratio})`;
      }
      scheduled = false;
    };
    window.addEventListener(
      "scroll",
      () => {
        if (scheduled) return;
        scheduled = true;
        window.requestAnimationFrame(updateScrollState);
      },
      { passive: true }
    );
    updateScrollState();
  }
})();
