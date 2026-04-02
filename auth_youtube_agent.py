import os
import google_auth_oauthlib.flow

def main():
    # Allow local HTTP for OAuth flow
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    
    client_secrets_file = "client_secret.json"
    if not os.path.exists(client_secrets_file):
        print(f"Error: {client_secrets_file} not found. Copy it from standalone-course-agent.")
        return

    # We need the youtube.upload scope to upload videos
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    
    # Initialize the OAuth 2.0 flow
    flow = google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file(
        client_secrets_file, scopes)
    
    # This will open your web browser to authenticate with Google
    print("Opening browser for Google Authentication...")
    print("IMPORTANT: Select the specific channel you want to use for Investment videos.")
    credentials = flow.run_local_server(port=0)
    
    # Save the credentials specifically for the youtube channel
    with open("token_youtube.json", "w") as f:
        f.write(credentials.to_json())
        
    print("\nAuthentication successful! 'token_youtube.json' has been generated.")

if __name__ == "__main__":
    main()
