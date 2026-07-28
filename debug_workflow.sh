#!/bin/bash
# Debug script for OnePlus 13 Kernel Builder workflow

set -e

REPO="Hipuu/OnePlus13-KernelBuilder"

echo "🔍 OnePlus 13 Kernel Builder - Debug Tool"
echo ""

# Function to get latest run
get_latest_run() {
    gh run list -R "$REPO" --workflow "Build OnePlus 13 Kernel" --limit 1 --json databaseId,status,conclusion -q '.[0]'
}

# Function to get run logs
get_run_logs() {
    local run_id=$1
    gh run view "$run_id" -R "$REPO" --log
}

# Function to get failed step logs
get_failed_logs() {
    local run_id=$1
    gh run view "$run_id" -R "$REPO" --log-failed
}

# Parse arguments
case "${1:-status}" in
    status)
        echo "📊 Latest workflow status:"
        RUN_INFO=$(get_latest_run)
        RUN_ID=$(echo "$RUN_INFO" | jq -r '.databaseId')
        STATUS=$(echo "$RUN_INFO" | jq -r '.status')
        CONCLUSION=$(echo "$RUN_INFO" | jq -r '.conclusion')

        echo "  Run ID: $RUN_ID"
        echo "  Status: $STATUS"
        echo "  Conclusion: $CONCLUSION"
        echo ""

        gh run view "$RUN_ID" -R "$REPO"
        ;;

    logs)
        echo "📋 Fetching full logs for run ${2:-latest}..."
        if [ -z "$2" ]; then
            RUN_ID=$(get_latest_run | jq -r '.databaseId')
        else
            RUN_ID=$2
        fi

        get_run_logs "$RUN_ID"
        ;;

    failed)
        echo "❌ Fetching failed step logs for run ${2:-latest}..."
        if [ -z "$2" ]; then
            RUN_ID=$(get_latest_run | jq -r '.databaseId')
        else
            RUN_ID=$2
        fi

        get_failed_logs "$RUN_ID"
        ;;

    watch)
        echo "👀 Watching workflow run ${2:-latest}..."
        if [ -z "$2" ]; then
            RUN_ID=$(get_latest_run | jq -r '.databaseId')
        else
            RUN_ID=$2
        fi

        gh run watch "$RUN_ID" -R "$REPO"
        ;;

    rerun)
        echo "🔄 Rerunning latest failed workflow..."
        RUN_ID=$(get_latest_run | jq -r '.databaseId')
        gh run rerun "$RUN_ID" -R "$REPO"
        ;;

    artifacts)
        echo "📦 Listing artifacts for run ${2:-latest}..."
        if [ -z "$2" ]; then
            RUN_ID=$(get_latest_run | jq -r '.databaseId')
        else
            RUN_ID=$2
        fi

        gh run view "$RUN_ID" -R "$REPO" --json artifacts -q '.artifacts[] | "[\(.name)] - \(.sizeInBytes / 1024 / 1024 | floor)MB - Expires: \(.expiredAt // "N/A")"'
        ;;

    download)
        echo "⬇️  Downloading artifacts for run ${2:-latest}..."
        if [ -z "$2" ]; then
            RUN_ID=$(get_latest_run | jq -r '.databaseId')
        else
            RUN_ID=$2
        fi

        mkdir -p artifacts
        gh run download "$RUN_ID" -R "$REPO" -D artifacts/
        echo "  Artifacts saved to: artifacts/"
        ;;

    *)
        echo "Usage: $0 <command> [run_id]"
        echo ""
        echo "Commands:"
        echo "  status          - Show latest workflow status"
        echo "  logs [run_id]   - Show full logs"
        echo "  failed [run_id] - Show failed step logs only"
        echo "  watch [run_id]  - Watch workflow in real-time"
        echo "  rerun           - Rerun latest failed workflow"
        echo "  artifacts [id]  - List artifacts"
        echo "  download [id]   - Download artifacts"
        echo ""
        echo "Examples:"
        echo "  $0 status"
        echo "  $0 logs 30423797772"
        echo "  $0 failed"
        echo "  $0 download"
        ;;
esac
