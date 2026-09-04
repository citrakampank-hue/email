// Cloudflare Worker - TempMail Email Handler
// Version: 3.0.0
// Author: Just-TnError

export default {
  async email(message, env, ctx) {
    try {
      // Get raw email
      const rawEmail = await message.raw();
      
      // Parse email headers
      const to = cleanEmail(message.to);
      const from = cleanEmail(message.from);
      const subject = message.headers.get("subject") || "(No Subject)";
      const date = message.headers.get("date") || new Date().toISOString();
      
      // Parse body
      const parsedBody = parseEmailBody(rawEmail);
      
      // Extract attachments (if any)
      const attachments = parseAttachments(rawEmail);
      
      // Build email data
      const emailData = {
        to: to,
        from: from,
        subject: subject,
        text: parsedBody.text,
        html: parsedBody.html,
        date: date,
        timestamp: new Date().toISOString(),
        attachments: attachments,
        size: rawEmail.length
      };
      
      // Forward to Railway
      const response = await fetch(`${env.RAILWAY_URL}/webhook/email`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Email-Key": env.EMAIL_WEBHOOK_KEY,
          "X-Worker-Version": "3.0.0"
        },
        body: JSON.stringify(emailData)
      });
      
      if (response.ok) {
        console.log(`✓ Email forwarded successfully: ${to}`);
      } else {
        console.error(`✗ Railway returned ${response.status}`);
        // Retry once
        await new Promise(resolve => setTimeout(resolve, 1000));
        const retryResponse = await fetch(`${env.RAILWAY_URL}/webhook/email`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Email-Key": env.EMAIL_WEBHOOK_KEY,
            "X-Worker-Version": "3.0.0",
            "X-Retry": "true"
          },
          body: JSON.stringify(emailData)
        });
        
        console.log(`Retry result: ${retryResponse.status}`);
      }
      
    } catch (error) {
      console.error(`✗ Worker error: ${error.message}`);
      throw error;
    }
  },
  
  async fetch(request, env) {
    const url = new URL(request.url);
    
    // Health check endpoint
    if (url.pathname === "/health") {
      return new Response(JSON.stringify({
        status: "healthy",
        worker: "tempmail-email-handler",
        version: "3.0.0",
        timestamp: new Date().toISOString()
      }), {
        headers: { "Content-Type": "application/json" }
      });
    }
    
    // Test endpoint
    if (url.pathname === "/test" && request.method === "POST") {
      const testData = await request.json();
      return new Response(JSON.stringify({
        received: true,
        data: testData
      }), {
        headers: { "Content-Type": "application/json" }
      });
    }
    
    return new Response("TempMail Worker Active", {
      headers: { "Content-Type": "text/plain" }
    });
  }
};

// Helper: Clean email address
function cleanEmail(email) {
  if (!email) return "";
  
  // Handle "Name <email@domain.com>" format
  const match = email.match(/<([^>]+)>/);
  if (match) {
    return match[1].toLowerCase();
  }
  
  return email.toLowerCase();
}

// Helper: Parse email body
function parseEmailBody(rawEmail) {
  let text = "";
  let html = "";
  
  // Try to find plain text part
  const textMatch = rawEmail.match(/Content-Type: text\/plain[\s\S]*?\r\n\r\n([\s\S]*?)(?:\r\n--boundary|$)/i);
  if (textMatch) {
    text = textMatch[1].trim();
  }
  
  // Try to find HTML part
  const htmlMatch = rawEmail.match(/Content-Type: text\/html[\s\S]*?\r\n\r\n([\s\S]*?)(?:\r\n--boundary|$)/i);
  if (htmlMatch) {
    html = htmlMatch[1].trim();
  }
  
  // If no multipart, use raw body
  if (!text && !html) {
    const bodyMatch = rawEmail.split("\r\n\r\n");
    if (bodyMatch.length > 1) {
      text = bodyMatch.slice(1).join("\r\n\r\n").trim();
    }
  }
  
  // If only HTML, strip tags for text
  if (!text && html) {
    text = html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
  }
  
  return { text, html };
}

// Helper: Parse attachments
function parseAttachments(rawEmail) {
  const attachments = [];
  const boundaryMatch = rawEmail.match(/boundary="?([^"\s]+)"?/i);
  
  if (!boundaryMatch) return attachments;
  
  const boundary = boundaryMatch[1];
  const parts = rawEmail.split(`--${boundary}`);
  
  for (const part of parts) {
    if (part.includes("Content-Disposition: attachment") || 
        part.includes('Content-Disposition: attachment;')) {
      
      const filenameMatch = part.match(/filename="?([^"\s]+)"?/i);
      const contentTypeMatch = part.match(/Content-Type: ([^\s;]+)/i);
      const contentTransferMatch = part.match(/Content-Transfer-Encoding: ([^\s]+)/i);
      
      if (filenameMatch) {
        const contentStart = part.indexOf("\r\n\r\n");
        let content = part.slice(contentStart + 4).trim();
        
        // Decode base64 if needed
        if (contentTransferMatch && contentTransferMatch[1].toLowerCase() === "base64") {
          try {
            content = atob(content.replace(/\s/g, ''));
          } catch (e) {
            // Keep as is if decode fails
          }
        }
        
        attachments.push({
          filename: filenameMatch[1],
          contentType: contentTypeMatch ? contentTypeMatch[1] : "application/octet-stream",
          size: content.length,
          content: content.slice(0, 100) // Only send preview, not full content
        });
      }
    }
  }
  
  return attachments.slice(0, 5); // Max 5 attachments
}