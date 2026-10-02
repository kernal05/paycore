export function money(minor, currency = "INR") {
  return new Intl.NumberFormat("en-IN", { style: "currency", currency }).format((minor || 0) / 100);
}

export function when(ts) {
  return new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(ts));
}

// Turns internal status codes into plain language + a badge tone.
export function statusInfo(status, fraudDecision, reason) {
  switch (status) {
    case "SETTLED":
    case "APPROVED_AND_SETTLED":
      return { label: "Completed", cls: "badge-pass" };
    case "REFUNDED":
      return { label: "Refunded", cls: "badge-pending" };
    case "FAILED":
      if (reason === "processor_declined") return { label: "Declined by bank", cls: "badge-fail" };
      if (reason === "manual_review_rejected") return { label: "Rejected by reviewer", cls: "badge-fail" };
      return { label: "Failed", cls: "badge-fail" };
    case "REVIEW":
      return { label: "Held for security review", cls: "badge-pending" };
    case "AUTHORIZED":
      return fraudDecision === "REVIEW"
        ? { label: "Held for security review", cls: "badge-pending" }
        : { label: "Authorized", cls: "badge-pending" };
    case "PROCESSING":
      return { label: "Processing", cls: "badge-pending" };
    case "UNKNOWN":
      return { label: "Confirming with bank", cls: "badge-pending" };
    case "MANUAL_REVIEW":
      return { label: "Needs manual review", cls: "badge-pending" };
    case "BLOCKED":
      return { label: "Blocked", cls: "badge-fail" };
    default:
      return { label: status || "Unknown", cls: "badge-pending" };
  }
}
