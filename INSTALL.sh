#!/bin/bash

# FIO Benchmark Helm Chart Installation Script

set -e

CHART_NAME="fio-benchmark"
RELEASE_NAME="my-fio-benchmark"
NAMESPACE="default"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}FIO Benchmark Helm Chart Installer${NC}"
echo "===================================="

# Check prerequisites
echo "Checking prerequisites..."

if ! command -v helm &> /dev/null; then
    echo -e "${RED}Error: helm is not installed${NC}"
    exit 1
fi

if ! command -v kubectl &> /dev/null; then
    echo -e "${RED}Error: kubectl is not installed${NC}"
    exit 1
fi

# Check cluster connectivity
if ! kubectl cluster-info &> /dev/null; then
    echo -e "${RED}Error: Cannot connect to cluster${NC}"
    exit 1
fi

echo -e "${GREEN}✓ All prerequisites met${NC}"

# Installation options
echo ""
echo "Installation Options:"
echo "1. Quick install (default values)"
echo "2. Custom install (interactive)"
echo "3. Install with custom FIO job file"
echo "4. Exit"
echo ""
read -p "Select option [1-4]: " option

case $option in
    1)
        echo "Installing with default values..."
        helm install $RELEASE_NAME ./$CHART_NAME --namespace $NAMESPACE
        ;;
    2)
        read -p "Number of pods [3]: " pods
        pods=${pods:-3}
        
        read -p "PVC size [10Gi]: " pvc_size
        pvc_size=${pvc_size:-10Gi}
        
        read -p "Image repository [your-registry.example.com/fio]: " image_repo
        image_repo=${image_repo:-your-registry.example.com/fio}
        
        read -p "Image tag [3.41]: " image_tag
        image_tag=${image_tag:-3.41}
        
        echo "Installing with custom values..."
        helm install $RELEASE_NAME ./$CHART_NAME \
            --namespace $NAMESPACE \
            --set replicaCount=$pods \
            --set pvc.size=$pvc_size \
            --set image.repository=$image_repo \
            --set image.tag=$image_tag
        ;;
    3)
        read -p "Path to FIO job file: " fio_file
        if [ ! -f "$fio_file" ]; then
            echo -e "${RED}Error: File not found${NC}"
            exit 1
        fi
        
        echo "Installing with custom FIO job..."
        helm install $RELEASE_NAME ./$CHART_NAME \
            --namespace $NAMESPACE \
            --set-file fioJob.file=$fio_file
        ;;
    4)
        echo "Exiting..."
        exit 0
        ;;
    *)
        echo -e "${RED}Invalid option${NC}"
        exit 1
        ;;
esac

echo ""
echo -e "${GREEN}Installation completed!${NC}"
echo ""
echo "Useful commands:"
echo "  View pods: kubectl get pods -l app=fio-benchmark -n $NAMESPACE"
echo "  View logs: kubectl logs -l app=fio-benchmark -n $NAMESPACE"
echo "  Uninstall: helm uninstall $RELEASE_NAME -n $NAMESPACE"
