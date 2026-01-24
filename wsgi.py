from app import create_app

app = create_app()

if __name__ == "__main__":
    # hanya untuk development (bukan Railway)
    app.run(host="0.0.0.0", port=8082, debug=True)
