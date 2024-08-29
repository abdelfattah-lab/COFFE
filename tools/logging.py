###############
# Log Functions
###############

def log(message):
    print(f"(!) {message}")
def log_error(message):
    print(f"[ERR] {message}")
    exit()
def log_list(lst):
    for item in lst:
        print(f"- {item}")
def get_time_str(time_now):
   return time_now.strftime('%d %b %Y, %I:%M:%S %p')